"""EPP capture acceptance with real sanitized OTLP publication."""

import gzip
import json

import pytest

from projects.cluster.tests.test_tracing_capture_toolbox import _record, _run_publish, _window
from projects.cluster.toolbox.capture_traces import main


def _request(trace_id, start_s):
    epp = _record(trace_id, "llmd-abc-epp", start_ns=start_s * 10**9)
    vllm = _record(trace_id, "llmd-abc-vllm", start_ns=start_s * 10**9 + 1)
    child = vllm["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    child["spanId"] = "0000000000000002"
    child["parentSpanId"] = "0000000000000001"
    return [epp, vllm]


def _summary(tmp_path, name):
    return json.loads((tmp_path / "out" / name / "traces/trace-summary.json").read_text())


def test_two_workloads_keep_epp_and_vllm_ids_and_parents(tmp_path):
    windows = [
        _window("a", 90, 150, str(tmp_path / "out/a")),
        _window("b", 190, 250, str(tmp_path / "out/b")),
    ]
    ctx = _run_publish(
        tmp_path,
        windows,
        [*_request("a" * 32, 100), *_request("b" * 32, 200)],
        roles=["vllm", "epp"],
    )
    assert ctx.status.value == "collected"
    for name in ("a", "b"):
        summary = _summary(tmp_path, name)
        assert summary["status"]["state"] == "collected"
        assert summary["config"]["instrumentation_roles"] == ["vllm", "epp"]
        assert summary["data"]["observed_services"] == ["llmd-abc-epp", "llmd-abc-vllm"]
        data = gzip.decompress((tmp_path / f"out/{name}/traces/otlp.jsonl.gz").read_bytes())
        spans = [
            span
            for line in data.splitlines()
            for rs in json.loads(line)["resourceSpans"]
            for ss in rs["scopeSpans"]
            for span in ss["spans"]
        ]
        assert {span["traceId"] for span in spans} == {name * 32}
        assert spans[1]["parentSpanId"] == spans[0]["spanId"]
        assert b"SENTINEL" not in data
    main.cleanup_raw_spool.__wrapped__(None, ctx)


def test_epp_in_another_window_does_not_satisfy_full_sampling(tmp_path):
    windows = [
        _window("a", 90, 150, str(tmp_path / "out/a")),
        _window("b", 190, 250, str(tmp_path / "out/b")),
    ]
    ctx = _run_publish(
        tmp_path,
        windows,
        [*_request("a" * 32, 100), _record("b" * 32, "llmd-abc-vllm", start_ns=200 * 10**9)],
        roles=["vllm", "epp"],
    )
    assert ctx.status.value == "failed"
    assert _summary(tmp_path, "a")["status"]["state"] == "collected"
    summary = _summary(tmp_path, "b")
    assert summary["status"]["state"] == "failed"
    assert "llmd-abc-epp" in summary["status"]["reason"]
    assert summary["data"]["observed_services"] == ["llmd-abc-vllm"]
    main.cleanup_raw_spool.__wrapped__(None, ctx)


@pytest.mark.parametrize("connected", [False, True])
def test_full_sampling_requires_propagation_not_just_service_presence(tmp_path, connected):
    records = _request("a" * 32, 100)
    if not connected:
        child = records[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        child["traceId"] = "b" * 32
        child.pop("parentSpanId")
    ctx = _run_publish(
        tmp_path,
        [_window("a", 90, 150, str(tmp_path / "out/a"))],
        records,
        roles=["vllm", "epp"],
    )
    assert ctx.status.value == ("collected" if connected else "failed")
    if not connected:
        assert "context propagation" in _summary(tmp_path, "a")["status"]["reason"]
    main.cleanup_raw_spool.__wrapped__(None, ctx)


def test_low_sampling_does_not_require_both_services(tmp_path):
    ctx = _run_publish(
        tmp_path,
        [_window("a", 90, 150, str(tmp_path / "out/a"))],
        [_record("a" * 32, "llmd-abc-epp", start_ns=100 * 10**9)],
        roles=["vllm", "epp"],
        sample_ratio=0.1,
    )
    assert ctx.status.value == "collected"
    main.cleanup_raw_spool.__wrapped__(None, ctx)
