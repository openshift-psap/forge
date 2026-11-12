"""ag_praxis tracing adapter.

This is the ag_praxis-owned layer that resolves tracing settings and builds
the capture identity. The pure instrumentation helpers live in
``projects.cluster.library.tracing``; this module only injects concrete,
ag_praxis-specific fields.

Producer instrumentation (injecting OTEL environment into the sim
LLMInferenceService or the operator-managed praxis extproc Deployments) is
deferred until their OTLP support is confirmed: the capture lifecycle runs
with no instrumentation roles and collects nothing until then.
"""

from __future__ import annotations

from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    TracingSettings,
)


def resolve_tracing() -> TracingSettings:
    """Resolve tracing settings from ag_praxis project configuration."""
    from projects.cluster.library.tracing.models import settings_from_config
    from projects.core.library import config

    return settings_from_config(config.project.get_config, section="tracing")


def capture_identity_for_run(
    *,
    job_id: str,
    capture_id: str,
    namespace: str,
    deployment_id: str,
) -> CaptureIdentity:
    """Build the ag_praxis capture identity."""
    return CaptureIdentity(
        job_id=job_id,
        capture_id=capture_id,
        project="ag_praxis",
        namespace=namespace,
        deployment_id=deployment_id,
    )


__all__ = [
    "capture_identity_for_run",
    "resolve_tracing",
]
