"""Caliper tracing configuration and disabled-behavior tests.

Covers the plan's Configuration and Disabled-behavior rows for the Caliper side:
``caliper.export.traces.enabled`` defaults to off, and a disabled export never
resolves a trace context (so no tracing module is imported and no native request
is made).
"""

from __future__ import annotations

from projects.caliper.orchestration.export import _build_trace_export_context
from projects.caliper.orchestration.export_config import CaliperOrchestrationExportConfig


def _export_config(**traces: object) -> CaliperOrchestrationExportConfig:
    cfg = {
        "from": "/tmp/artifacts",
        "backend": {"mlflow": {"enabled": True}},
        "traces": traces,
    }
    return CaliperOrchestrationExportConfig.model_validate(cfg)


def test_export_config_traces_defaults_off():
    cfg = _export_config()
    assert cfg.traces.enabled is False


def test_build_trace_export_context_is_none_when_disabled():
    # Disabled native export must not build a context (and thus not import the
    # tracing exporter or issue any request).
    assert _build_trace_export_context(_export_config(enabled=False), None) is None


def test_build_trace_export_context_uses_config_workspace_when_enabled():
    cfg = _export_config(enabled=True)
    context = _build_trace_export_context(cfg, {"workspace": "forge-llmd"})
    assert context is not None
    assert context.workspace == "forge-llmd"


def test_build_trace_export_context_empty_workspace_when_enabled():
    cfg = _export_config(enabled=True)
    context = _build_trace_export_context(cfg, None)
    assert context is not None
    assert context.workspace == ""
