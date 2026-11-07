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
    assert _build_trace_export_context(_export_config(enabled=False)) is None


def test_build_trace_export_context_is_a_marker_when_enabled():
    # The context is only an enabled marker: the tracking URI, workspace, and
    # credentials are resolved by the export code and passed to ingestion
    # explicitly, so no destination state can be left stale or empty here.
    cfg = _export_config(enabled=True)
    context = _build_trace_export_context(cfg)
    assert context is not None
    assert not getattr(context, "tracking_uri", "")
