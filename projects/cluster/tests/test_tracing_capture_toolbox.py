"""Capture toolbox tests with a synthetic local spool (no cluster).

Verifies the publish path end-to-end: sanitization runs before persistence, the
archive keeps native OTLP shape and original IDs, and traces are partitioned to
the correct per-window bundle.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from projects.cluster.toolbox.capture_traces import main as capture_main

SENTINEL = "SENTINEL_PROMPT_do_not_persist"


def _record(trace_hex: str, service: str, *, start_ns: int) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}},
                        {"key": "forge.capture_id", "value": {"stringValue": "cap-1"}},
                        {"key": "gen_ai.prompt", "value": {"stringValue": SENTINEL}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "vllm"},
                        "spans": [
                            {
                                "traceId": trace_hex,
                                "spanId": "0000000000000001",
                                "name": f"root {SENTINEL}",
                                "kind": 2,
                                "startTimeUnixNano": str(start_ns),
                                "endTimeUnixNano": str(start_ns + 1_000_000_000),
                                "attributes": [
                                    {
                                        "key": "gen_ai.completion",
                                        "value": {"stringValue": SENTINEL},
                                    },
                                    {
                                        "key": "http.request.method",
                                        "value": {"stringValue": "POST"},
                                    },
                                ],
                                "events": [
                                    {
                                        "name": f"ev {SENTINEL}",
                                        "attributes": [
                                            {"key": "cookie", "value": {"stringValue": SENTINEL}}
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


class _Ctx:
    """Minimal context object for direct task invocation."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _run_publish(tmp_path: Path, windows, records, *, roles=None, sample_ratio=1.0):
    spool = tmp_path / "raw-otlp.jsonl"
    spool.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=windows,
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=sample_ratio,
        collector_image="img",
        instrumentation_roles=roles or ["vllm"],
        interrupted=False,
        local_spool=spool,
    )
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    # Invoke the publish task body directly (bypassing the DSL decorators).
    capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)
    return ctx


def _window(test_id: str, start_s: float, end_s: float, artifact_dir: str):
    from datetime import UTC, datetime

    from projects.cluster.library.tracing.models import TraceWindow

    return TraceWindow(
        test_id=test_id,
        start=datetime.fromtimestamp(start_s, UTC).isoformat(),
        end=datetime.fromtimestamp(end_s, UTC).isoformat(),
        artifact_dir=artifact_dir,
        project="llm_d",
        namespace="ns",
        deployment_id="llmd-abc",
    )


def test_sanitize_before_persistence_and_partition(tmp_path: Path):
    records = [
        _record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a", start_ns=100 * 10**9),
        _record("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "forge-b", start_ns=200 * 10**9),
    ]
    windows = [
        _window("workload-a", 90, 150, str(tmp_path / "out" / "workload-a")),
        _window("workload-b", 190, 250, str(tmp_path / "out" / "workload-b")),
    ]

    _run_publish(tmp_path, windows, records)

    for window, expected_trace in (
        ("workload-a", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("workload-b", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ):
        bundle_dir = tmp_path / "out" / window / "traces"
        archive = bundle_dir / "otlp.jsonl.gz"
        assert archive.is_file()
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            text = handle.read()
        # Sanitization happened before persistence.
        assert SENTINEL not in text
        assert "gen_ai.prompt" not in text
        assert "gen_ai.completion" not in text
        assert "cookie" not in text
        # Native shape and original IDs preserved.
        partition = [json.loads(line) for line in text.splitlines() if line.strip()]
        assert len(partition) == 1
        span = partition[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["traceId"] == expected_trace
        assert span["kind"] == 2
        assert span["name"] == "<redacted>"  # name carried the sentinel
        assert span["events"][0]["name"] == "<redacted-event>"
        # Allowed attributes survive.
        keys = {a["key"] for a in span["attributes"]}
        assert "http.request.method" in keys

        summary = json.loads((bundle_dir / "trace-summary.json").read_text(encoding="utf-8"))
        assert summary["schema_version"] == 1
        assert summary["identity"]["test_id"] == window
        assert summary["data"]["trace_count"] == 1
        assert summary["data"]["span_count"] == 1
        assert summary["data"]["checksum"]["algorithm"] == "sha256"
        assert summary["time"]["drain_outcome"] == "graceful"
        assert summary["status"]["state"] == "collected"


def test_empty_spool_publishes_empty_bundles(tmp_path: Path):
    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[_window("workload-a", 90, 150, str(tmp_path / "out" / "workload-a"))],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=0.0,
        collector_image="img",
        instrumentation_roles=[],
        interrupted=False,
        local_spool=None,
    )
    capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)

    summary = json.loads(
        (tmp_path / "out" / "workload-a" / "traces" / "trace-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["status"]["state"] == "empty"
    assert summary["data"]["trace_count"] == 0


def test_interrupted_capture_is_recorded(tmp_path: Path):
    records = [_record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a", start_ns=100 * 10**9)]
    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[_window("workload-a", 90, 150, str(tmp_path / "out" / "workload-a"))],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=True,
        local_spool=None,
    )
    spool = tmp_path / "raw-otlp.jsonl"
    spool.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    ctx.local_spool = spool
    capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)

    summary = json.loads(
        (tmp_path / "out" / "workload-a" / "traces" / "trace-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["time"]["drain_outcome"] == "timeout"
    assert summary["status"]["state"] == "incomplete"
    assert summary["quality"]["interrupted"] is True


def test_raw_spool_transfer_stays_outside_artifact_root(tmp_path: Path, monkeypatch):
    """The unsanitized spool must never be written below the artifact directory."""
    output_dir = tmp_path / "artifacts"
    output_dir.mkdir()
    ctx = _Ctx(
        output_dir=output_dir,
        windows=[],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=False,
        spool_bytes=10,
        collector_pod="collector-0",
    )

    copied_to = []

    def fake_copy(_source, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(SENTINEL, encoding="utf-8")
        copied_to.append(Path(destination))

    monkeypatch.setattr(capture_main, "oc_cp_from_pod", fake_copy)
    monkeypatch.setattr(capture_main, "_spool_checksum", lambda *_a, **_k: "")
    capture_main.transfer_spool.__wrapped__(ctx, ctx)

    assert copied_to
    raw = copied_to[0]
    assert not raw.is_relative_to(output_dir)
    assert raw.read_text(encoding="utf-8") == SENTINEL

    capture_main.cleanup_raw_spool.__wrapped__(ctx, ctx)
    assert not raw.exists()
    assert not raw.parent.exists()


def test_max_bundle_bytes_is_enforced_before_publishing(tmp_path: Path):
    """A capture larger than the configured bound must fail, not publish."""
    records = [
        _record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a", start_ns=100 * 10**9),
    ]
    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[_window("workload-a", 90, 150, str(tmp_path / "out" / "workload-a"))],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=False,
        max_bundle_bytes=16,
    )
    spool = tmp_path / "raw-otlp.jsonl"
    spool.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    ctx.local_spool = spool

    import pytest

    from projects.cluster.toolbox.capture_traces.main import TraceCaptureError

    with pytest.raises(TraceCaptureError, match="max_bundle_bytes"):
        capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)

    assert not (tmp_path / "out" / "workload-a" / "traces" / "otlp.jsonl.gz").exists()


def test_sanitized_and_partitioned_spools_stay_private(tmp_path: Path):
    """Intermediates contain sanitized payloads but must live outside artifacts."""
    output_dir = tmp_path / "artifacts"
    records = [_record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a", start_ns=100 * 10**9)]
    ctx = _Ctx(
        output_dir=output_dir,
        windows=[_window("workload-a", 90, 150, str(output_dir / "workload-a"))],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=False,
        max_bundle_bytes=1024 * 1024,
    )
    spool = tmp_path / "raw-otlp.jsonl"
    spool.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    ctx.local_spool = spool

    capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)

    for attr in ("sanitized_temp_dir", "partition_temp_dir"):
        temp_dir = getattr(ctx, attr)
        assert Path(temp_dir).exists()
        assert not Path(temp_dir).is_relative_to(output_dir)

    capture_main.cleanup_raw_spool.__wrapped__(ctx, ctx)
    assert not ctx.sanitized_temp_dir.exists()
    assert not ctx.partition_temp_dir.exists()
    assert not spool.exists()


def test_settle_deadline_marks_capture_interrupted(tmp_path: Path, monkeypatch):
    """Expiry of settle_timeout_seconds must be recorded, not claimed graceful."""
    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[],
        namespace="ns",
        capture_id="cap-1",
        collector_pod="collector-0",
        interrupted=False,
        settle_timeout_seconds=300,
        settle_deadline=0.0,
    )

    monkeypatch.setattr(
        capture_main,
        "_spool_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not poll after deadline")),
    )

    result = capture_main.wait_for_settle.__wrapped__(ctx, ctx)

    assert "deadline" in result
    assert ctx.interrupted is True


def test_settle_within_deadline_does_not_mark_interrupted(tmp_path: Path, monkeypatch):
    import time

    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[],
        namespace="ns",
        capture_id="cap-1",
        collector_pod="collector-0",
        interrupted=False,
        settle_timeout_seconds=300,
        settle_deadline=time.monotonic() + 300,
        spool_bytes=10,
        last_size=None,
        stable_samples=0,
    )
    monkeypatch.setattr(capture_main, "_spool_bytes", lambda *_a, **_k: 10)

    result = capture_main.wait_for_settle.__wrapped__(ctx, ctx)

    assert result[0] is False
    assert "not yet stable" in result[1]
    assert ctx.interrupted is False


def test_truncated_final_record_is_dropped_and_counted(tmp_path: Path):
    """A crash-truncated last line must not discard the rest of the capture."""
    record = _record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a", start_ns=100 * 10**9)
    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[_window("workload-a", 90, 150, str(tmp_path / "out" / "workload-a"))],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=True,
        max_bundle_bytes=1024 * 1024,
    )
    spool = tmp_path / "raw-otlp.jsonl"
    spool.write_text(json.dumps(record) + "\n" + '{"resourceSpans": [', encoding="utf-8")
    ctx.local_spool = spool

    capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)

    summary = json.loads(
        (tmp_path / "out" / "workload-a" / "traces" / "trace-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["status"]["state"] == "incomplete"
    assert summary["time"]["drain_outcome"] == "timeout"
    assert summary["data"]["trace_count"] == 1
    assert summary["quality"]["partial_records"] == 1


def test_interior_corrupt_record_fails_capture(tmp_path: Path):
    """Corruption that is not the trailing record is a real validation failure."""
    record = _record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a", start_ns=100 * 10**9)
    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[_window("workload-a", 90, 150, str(tmp_path / "out" / "workload-a"))],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=False,
        max_bundle_bytes=1024 * 1024,
    )
    spool = tmp_path / "raw-otlp.jsonl"
    spool.write_text('{"resourceSpans": [\n' + json.dumps(record) + "\n", encoding="utf-8")
    ctx.local_spool = spool

    import pytest

    from projects.cluster.toolbox.capture_traces.main import TraceCaptureError

    with pytest.raises(TraceCaptureError, match="line 1"):
        capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)


def test_transfer_rejects_spool_checksum_mismatch(tmp_path: Path, monkeypatch):
    """Bytes alone are not enough: a mismatched hash must not be published."""
    output_dir = tmp_path / "artifacts"
    output_dir.mkdir()
    ctx = _Ctx(
        output_dir=output_dir,
        windows=[],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=False,
        spool_bytes=1,
        collector_pod="collector-0",
    )

    def fake_copy(_source, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("x", encoding="utf-8")

    monkeypatch.setattr(capture_main, "oc_cp_from_pod", fake_copy)
    monkeypatch.setattr(capture_main, "_spool_checksum", lambda *_a, **_k: "deadbeef")

    import pytest

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        capture_main.transfer_spool.__wrapped__(ctx, ctx)


def test_transfer_accepts_matching_checksum(tmp_path: Path, monkeypatch):
    output_dir = tmp_path / "artifacts"
    output_dir.mkdir()
    ctx = _Ctx(
        output_dir=output_dir,
        windows=[],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=False,
        spool_bytes=1,
        collector_pod="collector-0",
    )

    def fake_copy(_source, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("payload", encoding="utf-8")

    import hashlib

    monkeypatch.setattr(capture_main, "oc_cp_from_pod", fake_copy)
    monkeypatch.setattr(
        capture_main,
        "_spool_checksum",
        lambda *_a, **_k: hashlib.sha256(b"payload").hexdigest(),
    )

    capture_main.transfer_spool.__wrapped__(ctx, ctx)

    assert ctx.local_spool.read_text(encoding="utf-8") == "payload"


def test_spool_bytes_raises_on_unparsable_output(monkeypatch):
    """An unreadable or rotated spool must not look like an empty capture."""
    import pytest

    class _Result:
        stdout = "stat: cannot stat: No such file\n"

    monkeypatch.setattr(capture_main, "oc_exec", lambda *_a, **_k: _Result())

    with pytest.raises(RuntimeError, match="could not read collector spool size"):
        capture_main._spool_bytes("ns", "collector-0")


def test_spool_bytes_returns_zero_for_missing_file(monkeypatch):
    """The documented missing-file fallback stays 0, not a parse failure."""

    class _Result:
        stdout = "0\n"

    monkeypatch.setattr(capture_main, "oc_exec", lambda *_a, **_k: _Result())

    assert capture_main._spool_bytes("ns", "collector-0") == 0


def test_window_with_no_assigned_spans_is_recorded_empty(tmp_path: Path):
    """A two-window capture where one window receives no traces must not pass.

    The spool holds records (other windows get spans), so the run was not
    "empty" by spool bytes; the empty window's bundle is recorded empty and the
    overall status becomes EMPTY, which the full-sampling gate acts on.
    """
    records = [_record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "forge-a", start_ns=100 * 10**9)]
    ctx = _Ctx(
        output_dir=tmp_path / "out",
        windows=[
            _window("workload-a", 90, 150, str(tmp_path / "out" / "workload-a")),
            _window("workload-b", 160, 200, str(tmp_path / "out" / "workload-b")),
        ],
        capture_id="cap-1",
        namespace="ns",
        project="llm_d",
        job_id="job-1",
        deployment_id="llmd-abc",
        mode="standard",
        sample_ratio=1.0,
        collector_image="img",
        instrumentation_roles=["vllm"],
        interrupted=False,
        max_bundle_bytes=1024 * 1024,
    )
    spool = tmp_path / "raw-otlp.jsonl"
    spool.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    ctx.local_spool = spool

    capture_main.sanitize_and_publish.__wrapped__(ctx, ctx)

    assert ctx.status.value == "empty"

    a = json.loads(
        (tmp_path / "out" / "workload-a" / "traces" / "trace-summary.json").read_text(
            encoding="utf-8"
        )
    )
    b = json.loads(
        (tmp_path / "out" / "workload-b" / "traces" / "trace-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert a["status"]["state"] == "collected"
    assert a["data"]["span_count"] == 1
    # The non-empty window's bundle is retained on disk.
    assert (tmp_path / "out" / "workload-a" / "traces" / "otlp.jsonl.gz").is_file()
    assert b["status"]["state"] == "empty"
    assert b["data"]["span_count"] == 0
