"""Pure instrumentation helpers for FORGE distributed tracing.

These helpers accept concrete engine arguments, container dicts, and resource
attributes and return concrete changes. They never initialize configuration,
read vaults, or apply manifests — orchestration resolves settings and calls
these functions.

Adapted from Benchflow's ``tracing.py``. Deliberately *not* carried over:
fixed flush sleeps, catch-and-warn failures, namespace-global resource
ownership, and the filter that drops every span named ``GET``.
"""

from __future__ import annotations

import hashlib

from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    InstrumentationRole,
    TracingConfigError,
    TracingMode,
    TracingSettings,
)

# Environment variable names understood by OTLP-enabled producers.
SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
EXPORTER_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACES_EXPORTER_ENV = "OTEL_TRACES_EXPORTER"
TRACES_SAMPLER_ENV = "OTEL_TRACES_SAMPLER"
TRACES_SAMPLER_ARG_ENV = "OTEL_TRACES_SAMPLER_ARG"
RESOURCE_ATTRIBUTES_ENV = "OTEL_RESOURCE_ATTRIBUTES"

TRACING_ENV_KEYS = (
    SERVICE_NAME_ENV,
    EXPORTER_ENDPOINT_ENV,
    TRACES_EXPORTER_ENV,
    TRACES_SAMPLER_ENV,
    TRACES_SAMPLER_ARG_ENV,
    RESOURCE_ATTRIBUTES_ENV,
)

# vLLM / llm-d knobs.
VLLM_OTLP_ENDPOINT_ARG = "--otlp-traces-endpoint"
VLLM_DETAILED_ARG = "--collect-detailed-traces"
EPP_TRACING_ARG = "--tracing"

_COLLECTOR_SERVICE = "forge-otel-collector"
OTLP_GRPC_PORT = 4317
OTLP_HTTP_PORT = 4318


# A Service name is a DNS-1035 label: at most 63 characters.
_SERVICE_NAME_MAX = 63
_CAPTURE_DIGEST_LENGTH = 8
_CAPTURE_JOB_SLUG_MAX = _SERVICE_NAME_MAX - len(_COLLECTOR_SERVICE) - 1 - 1 - _CAPTURE_DIGEST_LENGTH


def build_capture_id(job_id: str, namespace: str) -> str:
    """Return the capture identity for one job in one namespace.

    The job slug is bounded so the derived collector Service name stays a
    valid DNS-1035 label; the digest keeps truncated job names distinct.
    """
    from projects.core.dsl.utils import slugify_identifier

    digest = hashlib.sha256(f"{job_id}:{namespace}".encode()).hexdigest()
    base = slugify_identifier(job_id, max_length=_CAPTURE_JOB_SLUG_MAX)
    return f"{base}-{digest[:_CAPTURE_DIGEST_LENGTH]}"


def collector_service_name(capture_id: str) -> str:
    """Return the per-capture collector Service name (ownership-scoped)."""
    return f"{_COLLECTOR_SERVICE}-{capture_id}"


def otlp_grpc_endpoint(namespace: str, capture_id: str) -> str:
    return f"http://{collector_service_name(capture_id)}.{namespace}.svc.cluster.local:{OTLP_GRPC_PORT}"


def otlp_http_endpoint(namespace: str, capture_id: str) -> str:
    return f"http://{collector_service_name(capture_id)}.{namespace}.svc.cluster.local:{OTLP_HTTP_PORT}"


def tracing_environment(
    settings: TracingSettings,
    identity: CaptureIdentity,
    role: InstrumentationRole | str,
    *,
    namespace: str,
) -> dict[str, str]:
    """Build the OTLP environment for a producer container.

    The endpoint is a cluster-internal, non-secret URL. No credentials are
    included; the collector needs none.
    """
    resource_attrs = identity.resource_attributes(role)
    joined = ",".join(f"{k}={v}" for k, v in resource_attrs.items())
    return {
        SERVICE_NAME_ENV: identity.service_name(role),
        EXPORTER_ENDPOINT_ENV: otlp_grpc_endpoint(namespace, identity.capture_id),
        TRACES_EXPORTER_ENV: "otlp",
        TRACES_SAMPLER_ENV: "parentbased_traceidratio",
        TRACES_SAMPLER_ARG_ENV: _format_ratio(settings.sample_ratio),
        RESOURCE_ATTRIBUTES_ENV: joined,
    }


def _format_ratio(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)


def _strip_cli_arg(args: list[str], flag: str) -> list[str]:
    """Remove ``--flag`` / ``--flag=value`` / ``--flag value`` occurrences."""
    output: list[str] = []
    index = 0
    while index < len(args):
        item = str(args[index])
        if item == flag:
            index += 1
            if index < len(args) and not str(args[index]).startswith("--"):
                index += 1
            continue
        if item.startswith(f"{flag}="):
            index += 1
            continue
        output.append(item)
        index += 1
    return output


def vllm_tracing_args(
    args: list[str],
    settings: TracingSettings,
    identity: CaptureIdentity,
    role: InstrumentationRole | str,
    *,
    namespace: str,
) -> list[str]:
    """Return vLLM args with tracing applied.

    ``--collect-detailed-traces=all`` is added only in detailed mode.
    """
    if not settings.enabled:
        return list(args)

    output = _strip_cli_arg(list(args), VLLM_OTLP_ENDPOINT_ARG)
    output = _strip_cli_arg(output, VLLM_DETAILED_ARG)
    output.append(f"{VLLM_OTLP_ENDPOINT_ARG}={otlp_grpc_endpoint(namespace, identity.capture_id)}")
    if settings.detailed:
        output.append(f"{VLLM_DETAILED_ARG}=all")
    return output


def epp_tracing_args(args: list[str]) -> list[str]:
    """Enable EPP tracing while preserving its complete scheduler arguments.

    EPP uses a Go boolean flag. Accept a bare flag or ``=true``; reject explicit
    overrides rather than silently changing a hand-written tracing policy.
    """
    output: list[str] = []
    for index, arg in enumerate(args):
        if arg in (EPP_TRACING_ARG, "-tracing"):
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                raise TracingConfigError("EPP tracing requires --tracing=true")
            continue
        if arg.startswith((f"{EPP_TRACING_ARG}=", "-tracing=")):
            if arg.split("=", 1)[1] != "true":
                raise TracingConfigError("EPP tracing conflicts with tracing.mode")
            continue
        output.append(arg)
    return [*output, f"{EPP_TRACING_ARG}=true"]


def apply_env_vars(
    env: list[dict[str, str]],
    additions: dict[str, str],
) -> list[dict[str, str]]:
    """Merge tracing env vars into a container env list.

    Existing entries for the same name are replaced so repeated application is
    idempotent.
    """
    result = [dict(item) for item in env if item.get("name") not in additions]
    result.extend({"name": name, "value": value} for name, value in additions.items())
    return result


def apply_vllm_additional_args(existing: str, tracing_args: list[str] | None) -> str:
    """Append tracing args to an existing ``VLLM_ADDITIONAL_ARGS`` string.

    Idempotent: any prior tracing args are removed first. Returns ``existing``
    unchanged when ``tracing_args`` is falsy.
    """
    if not tracing_args:
        return existing
    tokens = existing.split() if existing else []
    tokens = _strip_cli_arg(tokens, VLLM_OTLP_ENDPOINT_ARG)
    tokens = _strip_cli_arg(tokens, VLLM_DETAILED_ARG)
    tokens.extend(tracing_args)
    return " ".join(tokens)


def vllm_detailed_supported(engine: str, *, mode: TracingMode) -> bool:
    """Return whether detailed model-phase tracing is supported for the engine.

    Only vLLM supports ``--collect-detailed-traces``. Others must be rejected,
    never silently downgraded.
    """
    if mode is not TracingMode.DETAILED:
        return True
    if engine != "vllm":
        raise TracingConfigError(
            f"tracing.mode=detailed is not supported for engine '{engine}'; "
            "detailed model-phase tracing requires vLLM"
        )
    return True
