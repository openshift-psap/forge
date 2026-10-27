"""llm_d tracing instrumentation rendering tests.

Covers the plan's Rendering row for the validated path: idempotent injection
into the standard serving container and preservation of existing non-tracing
args and env. Unsupported cases are rejected: detailed mode for non-vLLM, and
any P/D or EPP/router topology (instrumentation helpers exist but are not
validated end-to-end).
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


def test_instrument_standard_container_sets_tracing_env():
    manifest = _manifest()
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    env = {e["name"]: e["value"] for e in manifest["spec"]["template"]["containers"][0]["env"]}
    assert env["OTEL_SERVICE_NAME"] == "llmd-abc-vllm"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]


def test_pd_topology_is_rejected_before_rendering():
    manifest = _manifest(pd=True)
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


def test_epp_scheduler_only_is_allowed_and_not_instrumented():
    """An EPP scheduler present without a routing proxy is allowed but untouched."""
    manifest = _manifest()
    manifest["spec"]["router"] = {
        "scheduler": {"template": {"containers": [{"name": "main", "env": []}]}},
    }
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    # EPP container is left exactly as rendered (no injected tracing env).
    epp_env = manifest["spec"]["router"]["scheduler"]["template"]["containers"][0].get("env", [])
    assert all(e.get("name") != "OTEL_SERVICE_NAME" for e in epp_env)
    # But the serving container is instrumented.
    serving_env = {
        e["name"]: e["value"] for e in manifest["spec"]["template"]["containers"][0]["env"]
    }
    assert serving_env["OTEL_SERVICE_NAME"] == "llmd-abc-vllm"


def test_instrumentation_is_idempotent():
    manifest = _manifest()
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    once = copy.deepcopy(manifest)
    instrument_manifest(manifest, settings=_settings(), identity=_identity(), namespace="ns")
    assert manifest == once


def test_disabled_manifest_is_unchanged():
    manifest = _manifest()
    before = copy.deepcopy(manifest)
    instrument_manifest(
        manifest,
        settings=TracingSettings(mode=TracingMode.OFF),
        identity=_identity(),
        namespace="ns",
    )
    assert manifest == before


def test_detailed_mode_rejects_non_vllm_engine():
    manifest = _manifest()
    with pytest.raises(TracingConfigError, match="detailed"):
        instrument_manifest(
            manifest,
            settings=_settings(TracingMode.DETAILED),
            identity=_identity(),
            namespace="ns",
            engine="trtllm",
        )


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


class _FakeProjectConfig:
    """Stand-in for config.project with the (path, default_value=...) contract."""

    def __init__(self, values: dict):
        self._values = values

    def get_config(self, path, default_value=..., **_kwargs):
        return self._values.get(path, default_value)


def test_resolve_tracing_defaults_off_without_section(monkeypatch):
    """With no tracing configuration, resolve_tracing returns off (no collector)."""
    from projects.core.library import config as config_mod
    from projects.llm_d.orchestration import tracing_adapter

    monkeypatch.setattr(config_mod, "project", _FakeProjectConfig({}))
    settings = tracing_adapter.resolve_tracing()
    assert settings.mode is TracingMode.OFF
    assert not settings.enabled


def test_resolve_tracing_reads_configured_mode(monkeypatch):
    """A configured tracing.mode is honored (standard enables tracing)."""
    from projects.core.library import config as config_mod
    from projects.llm_d.orchestration import tracing_adapter

    monkeypatch.setattr(
        config_mod,
        "project",
        _FakeProjectConfig({"tracing.mode": "standard", "tracing.sample_ratio": 0.5}),
    )
    settings = tracing_adapter.resolve_tracing()
    assert settings.mode is TracingMode.STANDARD
    assert settings.enabled


def _finalizer_deps(monkeypatch, status_value: str):
    """Wire the llm_d capture finalizer against a fake lifecycle."""
    from projects.cluster.library.tracing.models import CaptureIdentity, CollectionStatus
    from projects.llm_d.orchestration import test_phase, tracing_adapter

    settings = TracingSettings(mode=TracingMode.STANDARD, sample_ratio=1.0)
    monkeypatch.setattr(tracing_adapter, "resolve_tracing", lambda: settings)
    monkeypatch.setattr(
        tracing_adapter,
        "capture_identity_for_run",
        lambda **_kw: CaptureIdentity(
            job_id="job-1",
            capture_id="cap-1",
            project="llm_d",
            namespace="ns",
            deployment_id="llmd-abc",
        ),
    )
    monkeypatch.setattr(test_phase.runtime_config, "get_job_name", lambda: "job-1")
    monkeypatch.setattr(test_phase.runtime_config, "get_namespace", lambda: "ns")
    monkeypatch.setattr(test_phase, "_current_test_id", lambda: "test-1")

    from projects.cluster.library.tracing import lifecycle

    captured = {}

    def fake_capture(session, **_kw):
        return CollectionStatus(status_value)

    monkeypatch.setattr(lifecycle, "capture", fake_capture)
    monkeypatch.setattr(lifecycle, "record_window", lambda *a, **k: None)
    monkeypatch.setattr(
        lifecycle, "cleanup", lambda *_a, **_k: captured.setdefault("cleaned", True)
    )
    return test_phase, lifecycle, captured


def test_incomplete_capture_fails_requested_tracing(monkeypatch, tmp_path):
    """A drain timeout must fail tracing and preserve the collector for recovery."""
    notifications = []
    from projects.core.library import ci

    monkeypatch.setattr(
        ci,
        "add_notification_file",
        lambda name, message: notifications.append((name, message)) or True,
    )

    test_phase, _lifecycle, captured = _finalizer_deps(monkeypatch, "incomplete")
    with pytest.raises(RuntimeError, match="incomplete"):
        test_phase._capture_traces_finalizer(
            "llmisvc-1", (_utc(0), _utc(10)), capture_session=object()
        )

    assert notifications and notifications[0][0] == "tracing-collection-incomplete"
    assert not captured.get("cleaned"), "collector must be preserved for recovery"


def test_completed_capture_cleans_up(monkeypatch):
    notifications = []
    from projects.core.library import ci

    monkeypatch.setattr(
        ci, "add_notification_file", lambda name, message: notifications.append(name) or True
    )
    test_phase, _lifecycle, captured = _finalizer_deps(monkeypatch, "collected")

    test_phase._capture_traces_finalizer("llmisvc-1", (_utc(0), _utc(10)), capture_session=object())

    assert captured.get("cleaned") is True
    assert notifications == []


def _utc(offset_seconds: int):
    from datetime import UTC, datetime, timedelta

    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_seconds)
