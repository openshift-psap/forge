"""Regression probes from the final tracing verification."""

import gzip
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from projects.cluster.tests.test_tracing_capture_toolbox import (
    _record,
    _run_publish,
    _window,
    capture_main,
)


@pytest.mark.parametrize("exit_code", [0, 1, None])
def test_storage_reads_pod_init_status(monkeypatch, exit_code):
    from projects.cluster.toolbox.deploy_trace_collector import main

    monkeypatch.setattr(main, "_collector_pod", lambda ctx: "collector-pod")
    get = Mock(
        return_value={
            "status": {
                "initContainerStatuses": [
                    {
                        "name": "spool-probe",
                        "state": {"terminated": {"exitCode": exit_code}},
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(main, "oc_get_json", get)
    ctx = SimpleNamespace(identity=SimpleNamespace(namespace="ns"))
    if exit_code == 0:
        main.verify_storage.__wrapped__(None, ctx)
        assert ctx.collector_pod == "collector-pod"
    else:
        with pytest.raises(RuntimeError, match="not writable"):
            main.verify_storage.__wrapped__(None, ctx)
    get.assert_called_once_with("pod", name="collector-pod", namespace="ns")


@pytest.mark.parametrize("shape", ["arrayValue", "kvlistValue", "bytesValue"])
def test_nested_credentials_never_reach_archive(tmp_path, shape):
    record = _record("a" * 32, "forge-a", start_ns=100 * 10**9)
    value = {"stringValue": "Bearer SYNTHETIC_CREDENTIAL_12345"}
    wrapped = {
        shape: {
            "values": [value]
            if shape == "arrayValue"
            else [
                {"key": "server.address", "value": value},
            ]
        }
    }
    if shape == "bytesValue":
        import base64

        wrapped = {shape: base64.b64encode(b"SYNTHETIC_CREDENTIAL_12345").decode()}
    span = record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["attributes"] = [{"key": "vllm.queue.duration", "value": wrapped}]
    ctx = _run_publish(tmp_path, [_window("a", 90, 150, str(tmp_path / "out/a"))], [record])
    try:
        data = gzip.decompress((tmp_path / "out/a/traces/otlp.jsonl.gz").read_bytes())
        assert b"SYNTHETIC_CREDENTIAL" not in data
        assert b"bytesValue" not in data
        assert b"a" * 32 in data
    finally:
        capture_main.cleanup_raw_spool.__wrapped__(None, ctx)


@pytest.mark.parametrize("reason", ["ambiguous", "missing-root", "outside-window"])
def test_unassigned_diagnostics_survive_cleanup(tmp_path, reason):
    record = _record("b" * 32, "forge-a", start_ns=100 * 10**9)
    windows = [_window("a", 90, 150, str(tmp_path / "out/a"))]
    if reason == "ambiguous":
        windows.append(_window("b", 95, 160, str(tmp_path / "out/b")))
    elif reason == "missing-root":
        record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["parentSpanId"] = "1" * 16
    else:
        windows = [_window("a", 190, 250, str(tmp_path / "out/a"))]
    ctx = _run_publish(tmp_path, windows, [record])
    capture_main.cleanup_raw_spool.__wrapped__(None, ctx)
    diagnostics = tmp_path / "out/trace-diagnostics"
    attribution = json.loads((diagnostics / "attribution.json").read_text())
    assert attribution["unassigned"] == [{"trace_id": "b" * 32, "reason": reason}]
    payload = gzip.decompress((diagnostics / "unassigned.otlp.jsonl.gz").read_bytes())
    assert b"b" * 32 in payload
    assert b"SENTINEL" not in payload
    assert ctx.status.value == ("empty" if reason == "outside-window" else "failed")


def test_conflicting_capture_retains_sanitized_diagnostics(tmp_path):
    from projects.caliper.engine.tracing.analysis import TraceValidationError

    first = _record("a" * 32, "forge-a", start_ns=100 * 10**9)
    second = _record("a" * 32, "forge-a", start_ns=101 * 10**9)
    with pytest.raises(TraceValidationError, match="conflicting"):
        _run_publish(tmp_path, [_window("a", 90, 150, str(tmp_path / "out/a"))], [first, second])
    data = gzip.decompress((tmp_path / "out/trace-diagnostics/invalid.otlp.jsonl.gz").read_bytes())
    assert len(data.splitlines()) == 2
    assert b"SENTINEL" not in data
