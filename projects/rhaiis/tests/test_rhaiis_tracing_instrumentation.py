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


def _settings(mode: TracingMode = TracingMode.STANDARD) -> TracingSettings:
    return TracingSettings(mode=mode, sample_ratio=0.5)


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


def test_detailed_mode_rejects_non_vllm_engine():
    manifest = _vllm_sr()
    with pytest.raises(TracingConfigError, match="not supported for engine"):
        instrument_servingruntime(
            manifest,
            settings=_settings(TracingMode.DETAILED),
            identity=_identity(),
            namespace="ns",
            engine="sglang",
        )


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


def _patch_lifecycle(monkeypatch, status_value: str):
    from projects.cluster.library.tracing import lifecycle
    from projects.cluster.library.tracing.models import CollectionStatus
    from projects.rhaiis.orchestration import test_phase

    cleaned = {}
    monkeypatch.setattr(lifecycle, "capture", lambda *_a, **_k: CollectionStatus(status_value))
    monkeypatch.setattr(lifecycle, "cleanup", lambda *_a, **_k: cleaned.setdefault("called", True))
    return test_phase, cleaned


def test_incomplete_capture_is_a_warning_and_preserves_collector(monkeypatch):
    notifications = []
    from projects.core.library import ci

    monkeypatch.setattr(
        ci, "add_notification_file", lambda name, message: notifications.append(name) or True
    )
    test_phase, cleaned = _patch_lifecycle(monkeypatch, "incomplete")
    test_phase._warnings.clear()

    test_phase._finalize_trace_capture(object())

    assert notifications == ["tracing-collection-incomplete"]
    assert "Trace collection incomplete" in test_phase._warnings
    assert not cleaned.get("called"), "collector must be preserved for recovery"


def test_completed_capture_cleans_up(monkeypatch):
    test_phase, cleaned = _patch_lifecycle(monkeypatch, "collected")
    test_phase._warnings.clear()

    test_phase._finalize_trace_capture(object())

    assert cleaned.get("called") is True
    assert test_phase._warnings == []
