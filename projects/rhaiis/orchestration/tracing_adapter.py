"""rhaiis tracing adapter.

The rhaiis-owned layer that resolves tracing settings and applies
instrumentation to a rendered ServingRuntime manifest. The pure instrumentation
helpers live in ``projects.cluster.library.tracing``; this module only injects
concrete, rhaiis-specific fields.

rhaiis builds the vLLM container with an explicit ``command``/``args`` list
(there is no ``VLLM_ADDITIONAL_ARGS`` string), so the OTLP endpoint is appended
directly to ``args`` and the OTLP environment is merged into ``env``.

Only the vLLM engine is validated end-to-end. SGLang and TRT-LLM would need
their own instrumentation capability checks and smoke tests, so requesting
tracing for them is rejected rather than silently rendered or downgraded.
"""

from __future__ import annotations

import logging
from typing import Any

from projects.cluster.library.tracing.instrumentation import (
    TRACING_ENV_KEYS,
    apply_env_vars,
    tracing_environment,
    vllm_detailed_supported,
    vllm_tracing_args,
)
from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    InstrumentationRole,
    TracingConfigError,
    TracingSettings,
)

logger = logging.getLogger(__name__)

SUPPORTED_ENGINE = "vllm"
# The vLLM model-server container in the rhaiis ServingRuntime manifest.
SERVING_CONTAINER_NAME = "kserve-container"


def resolve_tracing() -> TracingSettings:
    """Resolve tracing settings from rhaiis project configuration."""
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
    """Build the rhaiis capture identity."""
    return CaptureIdentity(
        job_id=job_id,
        capture_id=capture_id,
        project="rhaiis",
        namespace=namespace,
        deployment_id=deployment_id,
    )


def _reject_conflicting_tracing_env(container: dict[str, Any], expected: dict[str, str]) -> None:
    """Reject a container whose tracing env conflicts with resolved settings."""
    for env in container.get("env", []) or []:
        name = env.get("name")
        if name not in TRACING_ENV_KEYS:
            continue
        if str(env.get("value", "")) != expected.get(name):
            raise TracingConfigError(
                f"container '{container.get('name')}' defines tracing environment "
                f"{name!r} with a conflicting value; remove it and enable tracing.mode instead"
            )


def instrument_servingruntime(
    manifest: dict[str, Any],
    *,
    settings: TracingSettings,
    identity: CaptureIdentity,
    namespace: str,
    engine: str = SUPPORTED_ENGINE,
) -> dict[str, Any]:
    """Apply tracing instrumentation to a rendered ServingRuntime manifest.

    Returns the manifest (mutated in place) so rendering stays a single pass.
    When tracing is off the manifest is returned unchanged. Any engine other
    than vLLM is rejected before the manifest is touched.
    """
    if not settings.enabled:
        return manifest

    if engine != SUPPORTED_ENGINE:
        raise TracingConfigError(
            f"tracing is not supported for engine '{engine}' in rhaiis; only "
            f"'{SUPPORTED_ENGINE}' has a validated instrumentation path. SGLang and "
            "TRT-LLM require their own capability checks and smoke tests before "
            "tracing can be enabled for them."
        )
    vllm_detailed_supported(engine, mode=settings.mode)

    containers = manifest.get("spec", {}).get("containers", []) or []
    # Only the model-server container may be instrumented: the vLLM OTLP flags
    # are engine-specific, so a sidecar that received them would crash on start.
    serving = next((c for c in containers if c.get("name") == SERVING_CONTAINER_NAME), None)
    if serving is None:
        raise TracingConfigError(
            f"ServingRuntime manifest has no '{SERVING_CONTAINER_NAME}' container to "
            "instrument; refusing to inject vLLM-only tracing flags into other containers"
        )

    additions = tracing_environment(
        settings, identity, InstrumentationRole.VLLM, namespace=namespace
    )
    _reject_conflicting_tracing_env(serving, additions)

    args = list(serving.get("args", []) or [])
    tracing_args = vllm_tracing_args(
        args, settings, identity, InstrumentationRole.VLLM, namespace=namespace
    )
    # vllm_tracing_args preserves the existing args and appends/replaces only
    # the tracing flags, so a hand-written endpoint cannot survive.
    serving["args"] = tracing_args
    serving["env"] = apply_env_vars(serving.get("env", []) or [], additions)

    return manifest


__all__ = [
    "SUPPORTED_ENGINE",
    "capture_identity_for_run",
    "instrument_servingruntime",
    "resolve_tracing",
]
