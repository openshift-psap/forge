"""rhaiis tracing instrumentation rendering tests.

Covers the plan's Rendering row for rhaiis: instrumentation of the vLLM
ServingRuntime container, preservation of existing non-tracing args and env, and
explicit rejection of unsupported engines (SGLang, TRT-LLM) rather than a silent
downgrade.
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
from projects.rhaiis.orchestration.manifests import build_servingruntime
from projects.rhaiis.orchestration.tracing_adapter import instrument_servingruntime


def _identity() -> CaptureIdentity:
    return CaptureIdentity(
        job_id="job-1",
        capture_id="cap-1",
        project="rhaiis",
        namespace="ns",
        deployment_id="rhaiis-abc",
    )


def _settings(
    mode: TracingMode = TracingMode.STANDARD, sample_ratio: float = 0.5
) -> TracingSettings:
    return TracingSettings(mode=mode, sample_ratio=sample_ratio)


def _vllm_sr() -> dict:
    return build_servingruntime(
        deployment_name="rhaiis-abc",
        namespace="ns",
        model_id="Qwen/Qwen3-0.6B",
        serving_image="image:latest",
        engine="vllm",
        engine_args={"max-model-len": 4096},
        engine_port=8080,
        storage_source="hf",
        gpu_count=1,
        env_vars={"KEEP_ME": "1"},
    )


def test_instrument_vllm_container_preserves_existing_and_adds_tracing():
    manifest = _vllm_sr()
    instrument_servingruntime(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    container = manifest["spec"]["containers"][0]
    assert "--max-model-len=4096" in container["args"]
    assert any(a.startswith("--otlp-traces-endpoint=") for a in container["args"])
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["KEEP_ME"] == "1"
    assert env["OTEL_SERVICE_NAME"] == "rhaiis-abc-vllm"


def test_instrumentation_is_idempotent():
    manifest = _vllm_sr()
    instrument_servingruntime(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    once = copy.deepcopy(manifest)
    instrument_servingruntime(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == once


def test_disabled_manifest_is_unchanged():
    manifest = _vllm_sr()
    before = copy.deepcopy(manifest)
    instrument_servingruntime(
        manifest,
        settings=TracingSettings(mode=TracingMode.OFF),
        identity=_identity(),
        namespace="ns",
    )
    assert manifest == before


@pytest.mark.parametrize("engine", ["sglang", "trtllm"])
def test_unsupported_engine_is_rejected(engine):
    manifest = _vllm_sr()
    before = copy.deepcopy(manifest)
    with pytest.raises(TracingConfigError, match="not supported for engine"):
        instrument_servingruntime(
            manifest,
            settings=_settings(),
            identity=_identity(),
            namespace="ns",
            engine=engine,
        )
    assert manifest == before


def test_detailed_mode_adds_detailed_flag_for_vllm():
    manifest = _vllm_sr()
    instrument_servingruntime(
        manifest,
        settings=_settings(TracingMode.DETAILED),
        identity=_identity(),
        namespace="ns",
        engine="vllm",
    )
    container = manifest["spec"]["containers"][0]
    assert "--collect-detailed-traces=all" in container["args"]


def test_manual_tracing_env_is_rejected():
    manifest = _vllm_sr()
    manifest["spec"]["containers"][0]["env"].append(
        {"name": "OTEL_SERVICE_NAME", "value": "hand-written"}
    )
    with pytest.raises(TracingConfigError, match="defines tracing environment"):
        instrument_servingruntime(
            manifest, settings=_settings(), identity=_identity(), namespace="ns"
        )


def test_only_the_model_server_container_is_instrumented():
    """A sidecar must not receive vLLM-only OTLP flags and env."""
    manifest = _vllm_sr()
    sidecar = {
        "name": "metrics-sidecar",
        "args": ["--listen=:9090"],
        "env": [{"name": "SIDECAR", "value": "1"}],
    }
    manifest["spec"]["containers"].append(sidecar)

    instrument_servingruntime(manifest, settings=_settings(), identity=_identity(), namespace="ns")

    serving = next(c for c in manifest["spec"]["containers"] if c["name"] == "kserve-container")
    assert any("--otlp-traces-endpoint" in a for a in serving["args"])
    # The sidecar is untouched.
    assert sidecar["args"] == ["--listen=:9090"]
    assert sidecar["env"] == [{"name": "SIDECAR", "value": "1"}]


def test_missing_model_server_container_is_rejected():
    """Rendering must fail rather than silently instrument nothing."""
    manifest = _vllm_sr()
    manifest["spec"]["containers"][0]["name"] = "not-the-server"

    with pytest.raises(TracingConfigError, match="no 'kserve-container'"):
        instrument_servingruntime(
            manifest, settings=_settings(), identity=_identity(), namespace="ns"
        )


def _session(sample_ratio: float, *, windows: tuple = ("window",)):
    """A minimal capture session exposing what the finalizer reads."""
    return type(
        "S", (), {"settings": _settings(sample_ratio=sample_ratio), "windows": list(windows)}
    )()


def _patch_lifecycle(monkeypatch, capture):
    from projects.cluster.library.tracing import lifecycle
    from projects.core.library import ci
    from projects.rhaiis.orchestration import test_phase

    notifications, cleanups = [], []
    monkeypatch.setattr(
        ci, "add_notification_file", lambda name, _message: notifications.append(name) or True
    )
    monkeypatch.setattr(lifecycle, "capture", capture)
    monkeypatch.setattr(lifecycle, "cleanup", lambda *_a, **_k: cleanups.append(True))
    test_phase._warnings.clear()
    return test_phase, notifications, cleanups


@pytest.mark.parametrize(
    ("outcome", "sample_ratio", "warning", "notification"),
    [
        ("collected", 0.5, None, None),
        # An empty capture is expected at low sampling, a misconfiguration at full sampling.
        ("empty", 0.1, None, None),
        (
            "empty",
            1.0,
            "Trace collection empty-at-full-sampling",
            "tracing-collection-empty-at-full-sampling",
        ),
        ("incomplete", 1.0, "Trace collection incomplete", "tracing-collection-incomplete"),
        ("capture-raises", 1.0, "Trace collection failed", "tracing-capture-failed"),
        # The run failed before any benchmark: nothing to capture.
        ("no-window", 1.0, None, None),
    ],
)
def test_capture_finalizer_always_removes_the_collector(
    monkeypatch, outcome, sample_ratio, warning, notification
):
    from projects.cluster.library.tracing.models import CollectionStatus

    def capture(*_a, **_k):
        if outcome == "no-window":
            pytest.fail("nothing to capture")
        if outcome == "capture-raises":
            raise RuntimeError("spool transfer failed")
        return CollectionStatus(outcome)

    test_phase, notifications, cleanups = _patch_lifecycle(monkeypatch, capture)
    windows = () if outcome == "no-window" else ("window",)

    test_phase._finalize_trace_capture(_session(sample_ratio, windows=windows))

    assert cleanups, "the collector must not be left behind"
    assert notifications == ([notification] if notification else [])
    assert test_phase._warnings == ([warning] if warning else [])


def test_cleanup_failure_is_notified(monkeypatch):
    from projects.cluster.library.tracing import lifecycle
    from projects.cluster.library.tracing.models import CollectionStatus

    test_phase, notifications, _cleanups = _patch_lifecycle(
        monkeypatch, lambda *_a, **_k: CollectionStatus("collected")
    )

    def failing_cleanup(*_a, **_k):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(lifecycle, "cleanup", failing_cleanup)

    test_phase._finalize_trace_capture(_session(0.5))

    assert notifications == ["tracing-cleanup-failed"]
    assert test_phase._warnings == ["Trace collector cleanup failed"]
