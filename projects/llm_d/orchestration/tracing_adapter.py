"""llm_d tracing adapter.

This is the llm_d-owned layer that resolves tracing settings and applies
instrumentation to a rendered LLMInferenceService manifest. The pure
instrumentation helpers live in ``projects.cluster.library.tracing``; this
module only injects concrete, llm_d-specific fields.

Instrumentation targets:
- the standard serving container (``spec.template.containers[main]``).
- EPP (``spec.router.scheduler.template.containers[main]``).

EPP needs a complete scheduler argument template: an args override replaces
controller defaults. Prefill/decode (P/D) and routing proxies are deferred and
rejected. Live producer validation remains a release gate.
"""

from __future__ import annotations

import copy
from typing import Any

from projects.cluster.library.tracing.instrumentation import (
    TRACING_ENV_KEYS,
    apply_env_vars,
    apply_vllm_additional_args,
    epp_tracing_args,
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

VLLM_ADDITIONAL_ARGS = "VLLM_ADDITIONAL_ARGS"
SERVING_CONTAINER_NAME = "main"


def resolve_tracing() -> TracingSettings:
    """Resolve tracing settings from llm_d project configuration."""
    from projects.cluster.library.tracing.models import settings_from_config
    from projects.core.library import config

    return settings_from_config(config.project.get_config, section="tracing")


def _reject_conflicting_tracing_env(container: dict[str, Any], expected: dict[str, str]) -> None:
    """Reject a container whose tracing env conflicts with resolved settings.

    Re-injecting the same values (idempotent re-rendering) is allowed; a
    differing value means a hand-written tracing configuration that would make
    the effective endpoint or sampling ambiguous.
    """
    for env in container.get("env", []) or []:
        name = env.get("name")
        if name not in TRACING_ENV_KEYS:
            continue
        if str(env.get("value", "")) != expected.get(name):
            raise TracingConfigError(
                f"container '{container.get('name')}' defines tracing environment "
                f"{name!r} with a conflicting value; remove it and enable tracing.mode instead"
            )


def _instrument_vllm_container(
    container: dict[str, Any],
    *,
    settings: TracingSettings,
    identity: CaptureIdentity,
    role: InstrumentationRole,
    namespace: str,
) -> None:
    expected_env = tracing_environment(settings, identity, role, namespace=namespace)
    _reject_conflicting_tracing_env(container, expected_env)

    existing = ""
    env = container.get("env", []) or []
    for entry in env:
        if entry.get("name") == VLLM_ADDITIONAL_ARGS:
            existing = str(entry.get("value", ""))
            break

    if not settings.enabled:
        return

    # Compute only the tracing arguments to append; vllm_tracing_args strips any
    # prior tracing flags from the passed list, and apply_vllm_additional_args
    # then appends them to the preserved existing arguments.
    tracing_args = vllm_tracing_args(
        [],
        settings,
        identity,
        role,
        namespace=namespace,
    )
    merged_args = apply_vllm_additional_args(existing, tracing_args)
    # If existing was empty, derive the args from the merged string.
    additions = tracing_environment(settings, identity, role, namespace=namespace)

    new_env = [entry for entry in env if entry.get("name") != VLLM_ADDITIONAL_ARGS]
    new_env.append({"name": VLLM_ADDITIONAL_ARGS, "value": merged_args})
    container["env"] = apply_env_vars(new_env, additions)


def _router_has_routing_proxy(router: dict[str, Any]) -> bool:
    """Return True when the router carries a routing-proxy container.

    The EPP/scheduler lives under ``router.scheduler.template.containers``; a P/D
    routing proxy lives directly under ``router.template.containers``. Its
    presence is rejected because proxy instrumentation is not validated yet.
    """
    containers = (router.get("template") or {}).get("containers") or []
    return bool(containers)


def instrumentation_roles(manifest: dict[str, Any]) -> list[str]:
    """Return the roles requested by a standard rendered deployment."""
    roles = [InstrumentationRole.VLLM.value]
    scheduler = (manifest.get("spec", {}).get("router") or {}).get("scheduler")
    if scheduler is not None:
        roles.append(InstrumentationRole.EPP.value)
    return roles


def _instrument_epp(
    scheduler: dict[str, Any],
    *,
    settings: TracingSettings,
    identity: CaptureIdentity,
    namespace: str,
) -> None:
    containers = (scheduler.get("template") or {}).get("containers") or []
    main = [container for container in containers if container.get("name") == "main"]
    if len(main) != 1 or not main[0].get("args"):
        raise TracingConfigError(
            "EPP tracing requires a scheduler template with a main container and complete "
            "args; overriding only --tracing would replace controller defaults. Use the "
            "approximate-prefix-cache or precise-prefix-cache profile, or provide a "
            "complete scheduler template. scheduler: {} is not supported with tracing."
        )
    container = main[0]
    # A tracing flag in command can take precedence over the generated args.
    if any(arg.lstrip("-").split("=", 1)[0] == "tracing" for arg in container.get("command", [])):
        raise TracingConfigError("EPP tracing flags in command conflict with tracing.mode")
    additions = tracing_environment(
        settings, identity, InstrumentationRole.EPP, namespace=namespace
    )
    additions["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
    _reject_conflicting_tracing_env(container, additions)
    for entry in container.get("env", []) or []:
        name = entry.get("name", "")
        if name.startswith("OTEL_EXPORTER_OTLP_") and name not in additions:
            raise TracingConfigError("EPP exporter overrides conflict with tracing.mode")
        if name == "OTEL_EXPORTER_OTLP_PROTOCOL" and entry.get("value") != "grpc":
            raise TracingConfigError("EPP requires the collector's gRPC exporter protocol")
    container["args"] = epp_tracing_args(container["args"])
    container["env"] = apply_env_vars(container.get("env", []) or [], additions)


def instrument_manifest(
    manifest: dict[str, Any],
    *,
    settings: TracingSettings,
    identity: CaptureIdentity,
    namespace: str,
    engine: str = "vllm",
) -> dict[str, Any]:
    """Apply tracing instrumentation to a rendered LLMInferenceService manifest.

    Returns the manifest (mutated in place for caller convenience) so rendering
    remains a single pass. When tracing is off the manifest is returned unchanged.

    Instrument vLLM and EPP through their declarative container templates.
    Validate a copy so unsupported topologies or conflicts leave the input intact.
    """
    if not settings.enabled:
        return manifest

    vllm_detailed_supported(engine, mode=settings.mode)

    updated = copy.deepcopy(manifest)
    spec = updated.setdefault("spec", {})

    if spec.get("prefill") is not None:
        raise TracingConfigError(
            "P/D (prefill/decode) tracing is deferred to a separate change; "
            "use a standard vLLM deployment with an explicit EPP template."
        )

    router = spec.get("router") or {}
    if _router_has_routing_proxy(router):
        raise TracingConfigError(
            "tracing is requested for a deployment with a routing-proxy container; "
            "routing-proxy instrumentation is implemented but not validated "
            "end-to-end, so FORGE refuses to render unsupported tracing. Add a "
            "smoke test to validate the routing proxy before enabling it."
        )

    scheduler = router.get("scheduler")
    if scheduler is not None:
        _instrument_epp(scheduler, settings=settings, identity=identity, namespace=namespace)

    # Standard serving container.
    standard_containers = spec.get("template", {}).get("containers", []) or []
    main = [
        container
        for container in standard_containers
        if container.get("name") == SERVING_CONTAINER_NAME
    ]
    if len(main) != 1:
        raise TracingConfigError("vLLM tracing requires exactly one main serving container")
    _instrument_vllm_container(
        main[0],
        settings=settings,
        identity=identity,
        role=InstrumentationRole.VLLM,
        namespace=namespace,
    )

    manifest.clear()
    manifest.update(updated)
    return manifest


def capture_identity_for_run(
    *,
    job_id: str,
    capture_id: str,
    namespace: str,
    deployment_id: str,
) -> CaptureIdentity:
    """Build the llm_d capture identity."""
    return CaptureIdentity(
        job_id=job_id,
        capture_id=capture_id,
        project="llm_d",
        namespace=namespace,
        deployment_id=deployment_id,
    )


__all__ = [
    "capture_identity_for_run",
    "instrument_manifest",
    "instrumentation_roles",
    "resolve_tracing",
]
