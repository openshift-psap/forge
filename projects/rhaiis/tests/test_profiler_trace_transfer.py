import gzip
import hashlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from projects.rhaiis.toolbox.copy_profiler_traces import main as transfer


def test_remote_chunk_reader_reads_only_requested_bytes(tmp_path) -> None:
    source = tmp_path / "trace.json"
    source.write_bytes(b"prefix\x00binary-suffix")

    result = subprocess.run(
        [sys.executable, "-c", transfer._READ_CHUNK_SCRIPT, str(source), "6", "7"],
        capture_output=True,
        check=True,
    )

    assert result.stdout == b"\x00binary"


def _mock_remote(monkeypatch, payload: bytes) -> None:
    def remote_text(_pod, _namespace, *command, **kwargs):
        if command[0] == "stat":
            return str(len(payload))
        if command[0] == "sha256sum":
            return hashlib.sha256(payload).hexdigest() + "  /tmp/trace.json"
        raise AssertionError(command)

    monkeypatch.setattr(transfer, "_remote_text", remote_text)


def test_copy_preserves_and_verifies_plain_rank0_trace(monkeypatch, tmp_path) -> None:
    payload = b'{"traceEvents": [1, 2, 3]}' * 100
    _mock_remote(monkeypatch, payload)

    def copy(command, *, capture_output, timeout, check):
        assert capture_output and 0 < timeout <= 30 and check is False
        assert command[-6:-3] == ["python3", "-c", transfer._READ_CHUNK_SCRIPT]
        assert command[-3:] == [f"/tmp/{filename}", "0", str(len(payload))]
        return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")

    filename = "trace_rank0_pid100_runprofile1_range500-503.json"
    monkeypatch.setattr(transfer.subprocess, "run", copy)

    copied = transfer._copy_one_trace("pod/model", "test", filename, tmp_path, 30)

    assert copied.name == filename
    assert copied.read_bytes() == payload
    assert not list(tmp_path.glob("*.partial"))


def test_truncated_chunk_is_retried_without_recopied_prefix(monkeypatch, tmp_path) -> None:
    payload = b'{"traceEvents": [1, 2, 3]}' * 100
    _mock_remote(monkeypatch, payload)
    monkeypatch.setattr(transfer, "_TRANSFER_CHUNK_BYTES", 1000)
    offsets = []

    def copy(command, *, capture_output, timeout, check):
        offset, length = map(int, command[-2:])
        offsets.append(offset)
        chunk = payload[offset : offset + length]
        if offset == 1000 and offsets.count(offset) == 1:
            chunk = chunk[:-8]
        return SimpleNamespace(returncode=0, stdout=chunk, stderr=b"")

    monkeypatch.setattr(transfer.subprocess, "run", copy)
    filename = "trace_rank0_pid100_runprofile4_range500-503.json"

    copied = transfer._copy_one_trace("pod/model", "test", filename, tmp_path, 30)
    assert offsets == [0, 1000, 1000, 2000]
    assert copied.read_bytes() == payload
    assert not list(tmp_path.glob("*.partial"))


def test_repeatedly_truncated_chunk_does_not_publish_trace(monkeypatch, tmp_path) -> None:
    payload = b'{"traceEvents": [1, 2, 3]}' * 100
    _mock_remote(monkeypatch, payload)
    attempts = []

    def copy(command, *, capture_output, timeout, check):
        attempts.append(int(command[-2]))
        return SimpleNamespace(returncode=0, stdout=payload[:-8], stderr=b"")

    monkeypatch.setattr(transfer.subprocess, "run", copy)
    filename = "trace_rank0_pid100_runprofile4_range500-503.json"

    with pytest.raises(RuntimeError, match="failed at offset 0 after 3 attempts"):
        transfer._copy_one_trace("pod/model", "test", filename, tmp_path, 30)
    assert attempts == [0, 0, 0]
    assert not list(tmp_path.iterdir())


def test_existing_gzip_trace_is_decoded_to_plain_json(monkeypatch, tmp_path) -> None:
    payload = b'{"traceEvents": [1, 2, 3]}'
    compressed = gzip.compress(payload)
    _mock_remote(monkeypatch, compressed)

    def copy(_command, *, capture_output, timeout, check):
        return SimpleNamespace(returncode=0, stdout=compressed, stderr=b"")

    monkeypatch.setattr(transfer.subprocess, "run", copy)
    filename = "trace_rank0_pid100_runprofile1_range500-503.json.gz"

    copied = transfer._copy_one_trace("pod/model", "test", filename, tmp_path, 30)
    assert copied.name == filename.removesuffix(".gz")
    assert copied.read_bytes() == payload
    assert not list(tmp_path.glob("*.partial"))


def test_invalid_gzip_trace_does_not_publish_json(monkeypatch, tmp_path) -> None:
    invalid = b"not a gzip trace"
    _mock_remote(monkeypatch, invalid)

    def copy(_command, *, capture_output, timeout, check):
        return SimpleNamespace(returncode=0, stdout=invalid, stderr=b"")

    monkeypatch.setattr(transfer.subprocess, "run", copy)

    with pytest.raises(gzip.BadGzipFile):
        transfer._copy_one_trace(
            "pod/model",
            "test",
            "trace_rank0_pid100_runprofile1_range500-503.json.gz",
            tmp_path,
            30,
        )
    assert not list(tmp_path.iterdir())


def test_checksum_mismatch_does_not_publish_trace(monkeypatch, tmp_path) -> None:
    payload = b'{"traceEvents": [1, 2, 3]}'
    _mock_remote(monkeypatch, b"X" + payload[1:])

    def copy(_command, *, capture_output, timeout, check):
        return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")

    monkeypatch.setattr(transfer.subprocess, "run", copy)

    with pytest.raises(RuntimeError, match="SHA-256 verification"):
        transfer._copy_one_trace(
            "pod/model",
            "test",
            "trace_rank0_pid100_runprofile1_range500-503.json",
            tmp_path,
            30,
        )
    assert not list(tmp_path.iterdir())


def test_copy_task_retries_transfer_failures() -> None:
    assert transfer.copy_traces._retry_config["attempts"] == 3
    assert transfer.copy_traces._retry_config["retry_on_exceptions"] is True
