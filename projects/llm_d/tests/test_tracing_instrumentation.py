"""llm_d tracing instrumentation rendering tests.

Covers standard vLLM and EPP rendering, configuration conflicts, and disabled
behavior. P/D and routing proxies are deferred to a separate change.
"""

from __future__ import annotations

import copy

import pytest

from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    TracingConfigError,
    TracingMode,
    TracingSettings,
)
from projects.llm_d.orchestration.tracing_adapter import instrument_manifest


def _identity() -> CaptureIdentity:
    return CaptureIdentity(
        job_id="job-1",
        capture_id="cap-1",
        project="llm_d",
        namespace="ns",
        deployment_id="llmd-abc",
    )


def _settings(mode: TracingMode = TracingMode.STANDARD) -> TracingSettings:
    return TracingSettings(mode=mode, sample_ratio=0.5)


def _manifest(*, pd: bool = False) -> dict:
    """A standard single-pod manifest; ``pd`` adds P/D and EPP/router topology."""
    spec: dict = {
        "template": {
            "containers": [
                {
                    "name": "main",
                    "env": [
                        {
                            "name": "VLLM_ADDITIONAL_ARGS",
                            "value": "--model=x --max-model-len=4096",
                        },
                        {"name": "KEEP_ME", "value": "1"},
                    ],
                }
            ]
        },
    }
    if pd:
        spec["prefill"] = {"template": {"containers": [{"name": "main", "env": []}]}}
        spec["router"] = {
            "scheduler": {"template": {"containers": [{"name": "main", "env": []}]}},
            "template": {"containers": [{"name": "main", "args": ["--foo=1"], "env": []}]},
        }
    return {"spec": spec}


def test_instrument_standard_container_preserves_existing_args():
    manifest = _manifest()
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    container = manifest["spec"]["template"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert "--model=x" in env["VLLM_ADDITIONAL_ARGS"]
    assert "--max-model-len=4096" in env["VLLM_ADDITIONAL_ARGS"]
    assert "--otlp-traces-endpoint" in env["VLLM_ADDITIONAL_ARGS"]
    assert env["KEEP_ME"] == "1"
    assert env["OTEL_SERVICE_NAME"] == "llmd-abc-vllm"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]


@pytest.mark.parametrize("prefill", [{}, {"template": {"containers": [{"name": "main"}]}}])
def test_pd_topology_is_rejected_before_rendering(prefill):
    manifest = _manifest(pd=True)
    manifest["spec"]["prefill"] = prefill
    before = copy.deepcopy(manifest)
    with pytest.raises(TracingConfigError, match="P/D"):
        instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    # The rejection happens before the manifest is touched.
    assert manifest == before


def test_routing_proxy_topology_is_rejected():
    manifest = _manifest()
    # A P/D routing proxy lives directly under router.template.containers.
    manifest["spec"]["router"] = {
        "template": {"containers": [{"name": "main", "args": ["--foo=1"]}]}
    }
    before = copy.deepcopy(manifest)
    with pytest.raises(TracingConfigError, match="routing-proxy"):
        instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == before


def _with_epp():
    manifest = _manifest()
    manifest["spec"]["router"] = {
        "scheduler": {
            "template": {
                "containers": [
                    {
                        "name": "main",
                        "args": ["--pool-name", "pool", "--config-text", "plugins: []\n"],
                        "env": [{"name": "TOKENIZER_CACHE_DIR", "value": "/tmp/cache"}],
                        "image": "example/epp:reviewed",
                    },
                    {"name": "metrics-sidecar", "args": ["--port=9090"]},
                ]
            }
        },
    }
    return manifest


@pytest.mark.parametrize("mode", [TracingMode.STANDARD, TracingMode.DETAILED])
@pytest.mark.parametrize("ratio", [0.0, 0.5, 1.0])
def test_epp_and_vllm_share_capture_endpoint_and_sampling(mode, ratio):
    manifest = _with_epp()
    before = copy.deepcopy(manifest)
    settings = TracingSettings(mode=mode, sample_ratio=ratio)
    instrument_manifest(manifest, settings=settings, identity=_identity(), namespace="ns")
    containers = manifest["spec"]["router"]["scheduler"]["template"]["containers"]
    epp = containers[0]
    original_epp = before["spec"]["router"]["scheduler"]["template"]["containers"][0]
    assert epp["args"] == [*original_epp["args"], "--tracing=true"]
    assert epp["image"] == original_epp["image"]
    assert containers[1] == before["spec"]["router"]["scheduler"]["template"]["containers"][1]
    epp_env = {entry["name"]: entry["value"] for entry in epp["env"]}
    serving_env = {
        entry["name"]: entry["value"]
        for entry in manifest["spec"]["template"]["containers"][0]["env"]
    }
    assert epp_env["OTEL_SERVICE_NAME"] == "llmd-abc-epp"
    assert serving_env["OTEL_SERVICE_NAME"] == "llmd-abc-vllm"
    for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG"):
        assert epp_env[key] == serving_env[key]
    assert epp_env["OTEL_TRACES_SAMPLER"] == "parentbased_traceidratio"
    assert float(epp_env["OTEL_TRACES_SAMPLER_ARG"]) == ratio
    assert epp_env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"
    assert "forge.capture_id=cap-1" in epp_env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "forge.component.role=epp" in epp_env["OTEL_RESOURCE_ATTRIBUTES"]
    assert epp_env["TOKENIZER_CACHE_DIR"] == "/tmp/cache"
    assert not any("detailed" in arg for arg in epp["args"])


@pytest.mark.parametrize("flag", ["--tracing", "--tracing=true", "-tracing=true"])
def test_epp_reinjection_is_idempotent(flag):
    manifest = _with_epp()
    manifest["spec"]["router"]["scheduler"]["template"]["containers"][0]["args"].append(flag)
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    once = copy.deepcopy(manifest)
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == once
    assert (
        manifest["spec"]["router"]["scheduler"]["template"]["containers"][0]["args"].count(
            "--tracing=true"
        )
        == 1
    )


@pytest.mark.parametrize("args", [["--tracing=false"], ["-tracing=false"], ["--tracing", "false"]])
def test_epp_tracing_flag_conflict_leaves_manifest_unchanged(args):
    manifest = _with_epp()
    manifest["spec"]["router"]["scheduler"]["template"]["containers"][0]["args"].extend(args)
    before = copy.deepcopy(manifest)
    with pytest.raises(TracingConfigError, match="EPP tracing"):
        instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == before


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://other:4317"},
        {"name": "OTEL_SERVICE_NAME", "value": "other"},
        {"name": "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "value": "http://other:4317"},
        {"name": "OTEL_EXPORTER_OTLP_PROTOCOL", "value": "http/protobuf"},
        {
            "name": "OTEL_TRACES_SAMPLER_ARG",
            "valueFrom": {"configMapKeyRef": {"name": "c", "key": "ratio"}},
        },
    ],
)
def test_epp_exporter_conflicts_leave_manifest_unchanged(entry):
    manifest = _with_epp()
    manifest["spec"]["router"]["scheduler"]["template"]["containers"][0]["env"].append(entry)
    before = copy.deepcopy(manifest)
    with pytest.raises(TracingConfigError):
        instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == before


@pytest.mark.parametrize("scheduler", [{}, {"template": {"containers": [{"name": "main"}]}}])
def test_default_scheduler_args_are_never_replaced_by_tracing_only(scheduler):
    manifest = _manifest()
    manifest["spec"]["router"] = {"scheduler": scheduler}
    before = copy.deepcopy(manifest)
    with pytest.raises(TracingConfigError, match="complete args"):
        instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == before


def _fake_project_config(values: dict):
    return type("P", (), {"get_config": staticmethod(lambda key, *_a, **_k: values[key])})


def test_capture_records_the_roles_of_the_rendered_manifest(monkeypatch, tmp_path):
    import yaml

    from projects.cluster.library.tracing import lifecycle
    from projects.llm_d.orchestration import test_phase, tracing_adapter

    monkeypatch.setattr(
        test_phase.config,
        "project",
        _fake_project_config({"runtime.kserve.reuse_existing": False}),
    )
    monkeypatch.setattr(tracing_adapter, "resolve_tracing", _settings)
    monkeypatch.setattr(test_phase, "_tracing_job_id", lambda: "job-1")
    monkeypatch.setattr(test_phase.runtime_config, "get_namespace", lambda: "ns")
    calls = []
    monkeypatch.setattr(lifecycle, "start_capture", lambda **kw: calls.append(kw))
    path = tmp_path / "manifest.yaml"
    for manifest, roles in [(_manifest(), ["vllm"]), (_with_epp(), ["vllm", "epp"])]:
        path.write_text(yaml.safe_dump(manifest))
        test_phase._start_trace_capture("llmd-abc", manifest_path=path)
        assert calls[-1]["roles"] == roles


def test_tracing_rejects_reusing_an_untraced_service(monkeypatch, tmp_path):
    """A reused service keeps its untraced manifest, so the capture would be empty."""
    from projects.cluster.library.tracing import lifecycle
    from projects.llm_d.orchestration import test_phase, tracing_adapter

    monkeypatch.setattr(
        test_phase.config,
        "project",
        _fake_project_config({"runtime.kserve.reuse_existing": True}),
    )
    monkeypatch.setattr(tracing_adapter, "resolve_tracing", _settings)
    monkeypatch.setattr(
        lifecycle, "start_capture", lambda **_kw: pytest.fail("no collector may be deployed")
    )

    with pytest.raises(TracingConfigError, match="reuse_existing"):
        test_phase._start_trace_capture("llmd-abc", manifest_path=tmp_path / "manifest.yaml")


def test_instrumentation_is_idempotent():
    manifest = _manifest()
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    once = copy.deepcopy(manifest)
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == once


@pytest.mark.parametrize("build", [_manifest, _with_epp])
def test_disabled_manifest_is_unchanged(build):
    manifest = build()
    before = copy.deepcopy(manifest)
    instrument_manifest(
        manifest,
        settings=TracingSettings(mode=TracingMode.OFF),
        identity=_identity(),
        namespace="ns",
    )
    assert manifest == before


def test_detailed_mode_adds_detailed_flag_for_vllm():
    manifest = _manifest()
    instrument_manifest(
        manifest,
        settings=_settings(TracingMode.DETAILED),
        identity=_identity(),
        namespace="ns",
        engine="vllm",
    )
    env = {e["name"]: e["value"] for e in manifest["spec"]["template"]["containers"][0]["env"]}
    assert "--collect-detailed-traces=all" in env["VLLM_ADDITIONAL_ARGS"]


def test_manual_tracing_env_is_rejected():
    manifest = _manifest()
    manifest["spec"]["template"]["containers"][0]["env"].append(
        {"name": "OTEL_SERVICE_NAME", "value": "hand-written"}
    )
    with pytest.raises(TracingConfigError, match="defines tracing environment"):
        instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")


def _utc(offset_seconds: int):
    from datetime import UTC, datetime, timedelta

    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_seconds)


@pytest.mark.parametrize(
    ("outcome", "sample_ratio", "error", "notification"),
    [
        ("collected", 1.0, None, None),
        # An empty capture is expected at low sampling, a misconfiguration at full sampling.
        ("empty", 0.1, None, None),
        ("empty", 1.0, "empty-at-full-sampling", "tracing-collection-empty-at-full-sampling"),
        ("incomplete", 1.0, "incomplete", "tracing-collection-incomplete"),
        ("capture-raises", 1.0, "requested trace collection failed", "tracing-capture-failed"),
        # Writing the benchmark window fails before the capture starts.
        ("window-raises", 1.0, "requested trace collection failed", "tracing-capture-failed"),
        # The test failed before the benchmark: nothing to capture.
        ("no-window", 1.0, None, None),
    ],
)
def test_capture_finalizer_always_removes_the_collector(
    monkeypatch, outcome, sample_ratio, error, notification
):
    from projects.cluster.library.tracing import lifecycle
    from projects.cluster.library.tracing.models import CollectionStatus
    from projects.core.library import ci
    from projects.llm_d.orchestration import test_phase

    notifications, cleanups = [], []
    monkeypatch.setattr(
        ci, "add_notification_file", lambda name, _message: notifications.append(name) or True
    )
    monkeypatch.setattr(test_phase, "_current_test_id", lambda: "test-1")
    monkeypatch.setattr(lifecycle, "cleanup", lambda *_a, **_k: cleanups.append(True))

    def record_window(*_a, **_k):
        if outcome == "window-raises":
            raise OSError("cannot write trace-windows.json")

    def capture(*_a, **_k):
        if outcome in ("no-window", "window-raises"):
            pytest.fail("nothing to capture")
        if outcome == "capture-raises":
            raise RuntimeError("spool transfer failed")
        return CollectionStatus(outcome)

    monkeypatch.setattr(lifecycle, "record_window", record_window)
    monkeypatch.setattr(lifecycle, "capture", capture)
    settings = TracingSettings(mode=TracingMode.STANDARD, sample_ratio=sample_ratio)
    session = type("S", (), {"settings": settings})()
    window = None if outcome == "no-window" else (_utc(0), _utc(10))

    if error:
        with pytest.raises(RuntimeError, match=error):
            test_phase._capture_traces_finalizer(window, capture_session=session)
    else:
        test_phase._capture_traces_finalizer(window, capture_session=session)

    assert cleanups, "the collector must not be left behind"
    assert notifications == ([notification] if notification else [])


def test_finalizers_reconcile_traces_when_the_test_failed_before_the_benchmark(monkeypatch):
    """The trace finalizer runs whenever a collector was started."""
    from projects.llm_d.orchestration import test_phase

    calls = []
    monkeypatch.setattr(test_phase.runtime_config, "get_namespace", lambda: "ns")
    monkeypatch.setattr(
        test_phase.runtime_config,
        "get_platform_config",
        lambda: {"artifacts": {"capture_namespace_events": False}},
    )
    for name in (
        "capture_inference_service_state",
        "write_endpoint_url",
        "capture_namespace_events_after_test",
    ):
        monkeypatch.setattr(test_phase, name, lambda **_kw: None)
    monkeypatch.setattr(test_phase, "cleanup_test_resources", lambda **_kw: calls.append("cleanup"))
    monkeypatch.setattr(
        test_phase,
        "_capture_traces_finalizer",
        lambda **kw: calls.append(("traces", kw["benchmark_times"])),
    )

    test_phase.run_finalizers(
        None, "llmisvc-1", None, None, benchmark_times=None, capture_session=object()
    )

    assert calls == [("traces", None), "cleanup"]


@pytest.mark.parametrize(
    ("failure", "run_test_finalizers", "expected"),
    [
        # The finalizers capture traces and remove the collector.
        ("error", True, ["finalizers"]),
        # Skipped finalizers must not leak the collector and its PVC.
        ("error", False, ["remove-collector"]),
        ("interrupt", True, ["remove-collector"]),
    ],
)
def test_collector_is_removed_when_finalizers_are_skipped(
    monkeypatch, tmp_path, failure, run_test_finalizers, expected
):
    import signal

    from projects.core.library.run import SignalInterrupt
    from projects.llm_d.orchestration import test_phase

    calls = []
    settings = {
        "runtime.kserve.dry_run": False,
        "runtime.run_test_finalizers": run_test_finalizers,
        "platform.cluster.namespace.labels": {},
    }
    monkeypatch.setattr(test_phase.config, "project", _fake_project_config(settings))
    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(test_phase.runtime_config, "get_namespace", lambda: "ns")
    monkeypatch.setattr(
        test_phase.runtime_config,
        "get_platform_config",
        lambda: {"inference_service": {"name": "llmisvc"}},
    )
    monkeypatch.setattr(test_phase.runtime_config, "get_deployment_profile_name", lambda: "p")
    for name in (
        "ensure_namespace",
        "ensure_kueue_local_queue",
        "cleanup_existing_resources",
        "validate_user_workload_monitoring",
        "prepare_user_workload_monitoring",
        "create_test_labels",
        "update_test_labels_with_timing",
        "update_test_labels_with_status",
    ):
        monkeypatch.setattr(test_phase, name, lambda *_a, **_k: None)
    monkeypatch.setattr(test_phase.validate_gpu_operator_dcgm, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(
        test_phase,
        "build_inference_service_manifest",
        lambda name: (tmp_path / "manifest.yaml", name),
    )
    monkeypatch.setattr(test_phase, "_start_trace_capture", lambda *_a, **_k: "session")

    def failing_deploy(*_a, **_k):
        if failure == "interrupt":
            raise SignalInterrupt(signal.SIGTERM, None)
        raise RuntimeError("deploy failed")

    monkeypatch.setattr(test_phase, "deploy_inference_service_from_manifest", failing_deploy)

    def fake_finalizers(_url, _name, primary_exc, finalizer_exc, **_kw):
        calls.append("finalizers")
        return primary_exc, finalizer_exc

    monkeypatch.setattr(test_phase, "run_finalizers", fake_finalizers)
    monkeypatch.setattr(
        test_phase,
        "_remove_trace_collector_without_capture",
        lambda session: calls.append("remove-collector"),
    )

    with pytest.raises((RuntimeError, SignalInterrupt)):
        test_phase.do_test()

    assert calls == expected
