"""Typed models and validation for FORGE distributed tracing.

These types are shared by project orchestration (which resolves configuration),
toolbox commands (which receive explicit typed inputs), and Caliper export
(which validates bundles). They perform no I/O and import no project
orchestration.

Configuration validation never coerces values: malformed YAML types are
rejected rather than silently converted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Bump when the trace-summary schema changes in an incompatible way.
SUMMARY_SCHEMA_VERSION = 1

# Bump when the reviewed sanitization allowlist or redaction rules change.
SANITIZATION_POLICY_VERSION = 2


class TracingConfigError(ValueError):
    """Raised when tracing configuration is missing, malformed, or unsupported."""


class TracingMode(StrEnum):
    """Tracing mode selected by ``tracing.mode``."""

    OFF = "off"
    STANDARD = "standard"
    DETAILED = "detailed"

    @property
    def enabled(self) -> bool:
        return self is not TracingMode.OFF

    @property
    def detailed(self) -> bool:
        return self is TracingMode.DETAILED


# Collector image is pinned in the first implementation stage. The file
# exporter used for the spool is alpha, so an exact digest is a release gate:
# see docs/tracing/OPERATING.md ("Collector image and file contract").
DEFAULT_COLLECTOR_IMAGE = (
    "ghcr.io/open-telemetry/opentelemetry-collector-releases/"
    "opentelemetry-collector-contrib:0.115.1"
)
DEFAULT_STORAGE_SIZE = "10Gi"
DEFAULT_READY_TIMEOUT_SECONDS = 300
DEFAULT_SETTLE_TIMEOUT_SECONDS = 300
DEFAULT_PVC_NAME = "forge-trace-spool"
# Size beyond which the transfer/publish step refuses to load the whole archive
# into memory and streams it instead. Recorded so operators can tune storage.
DEFAULT_MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class TracingSettings:
    """Fully validated tracing settings for one job."""

    mode: TracingMode = TracingMode.OFF
    sample_ratio: float = 0.1
    collector_image: str = DEFAULT_COLLECTOR_IMAGE
    storage_size: str = DEFAULT_STORAGE_SIZE
    storage_class: str | None = None
    ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS
    settle_timeout_seconds: int = DEFAULT_SETTLE_TIMEOUT_SECONDS
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    resource_cpu_request: str = "200m"
    resource_cpu_limit: str = "1"
    resource_memory_request: str = "256Mi"
    resource_memory_limit: str = "1Gi"
    sanitization_policy_version: int = SANITIZATION_POLICY_VERSION

    @property
    def enabled(self) -> bool:
        return self.mode.enabled

    @property
    def detailed(self) -> bool:
        return self.mode.detailed


class InstrumentationRole(StrEnum):
    """Reviewed component roles that FORGE knows how to instrument."""

    VLLM_DECODE = "vllm-decode"
    VLLM_PREFILL = "vllm-prefill"
    VLLM = "vllm"
    EPP = "epp"
    ROUTING_PROXY = "routing-proxy"

    @property
    def normalized(self) -> str:
        return self.value


class TraceComponent(StrEnum):
    """Component identity recorded on spans and in summaries."""

    VLLM = "vllm"
    EPP = "epp"
    ROUTING_PROXY = "routing-proxy"


@dataclass(frozen=True)
class CaptureIdentity:
    """The three FORGE identities plus the deployment scope.

    ``service.name`` is derived from ``deployment_id`` and ``role`` so a single
    reused deployment keeps a stable identity across sequential workloads.
    """

    job_id: str
    capture_id: str
    project: str
    namespace: str
    deployment_id: str
    test_id: str = ""

    def service_name(self, role: InstrumentationRole | str) -> str:
        role_value = role.value if isinstance(role, InstrumentationRole) else str(role)
        return build_service_name(self.deployment_id, role_value)

    def resource_attributes(self, role: InstrumentationRole | str) -> dict[str, str]:
        return {
            "forge.job_id": self.job_id,
            "forge.capture_id": self.capture_id,
            "forge.project": self.project,
            "forge.namespace": self.namespace,
            "forge.deployment_id": self.deployment_id,
            "forge.component.role": str(
                role.value if isinstance(role, InstrumentationRole) else role
            ),
        }


MAX_SERVICE_NAME_LENGTH = 63


def build_service_name(deployment_id: str, role: str) -> str:
    """Build a unique, deployment-scoped ``service.name`` for a component role."""
    base = f"{deployment_id}-{role}"
    if len(base) <= MAX_SERVICE_NAME_LENGTH:
        return base
    # Keep the trailing role so span filters remain usable.
    keep = MAX_SERVICE_NAME_LENGTH - len(role) - 1
    return f"{deployment_id[:keep]}-{role}"


class CollectionStatus(StrEnum):
    """Collection outcome recorded in a bundle summary."""

    COLLECTED = "collected"
    EMPTY = "empty"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class DrainOutcome(StrEnum):
    """How the collector spool was drained before the final snapshot."""

    GRACEFUL = "graceful"
    TIMEOUT = "timeout"
    NOT_ATTEMPTED = "not-attempted"


@dataclass
class CollectionQuality:
    """Capture-quality indicators, kept separate from delivery completeness."""

    rootless_traces: int = 0
    missing_parent_spans: int = 0
    multi_service_traces: int = 0
    duplicate_spans: int = 0
    conflicting_duplicates: int = 0
    unassigned_traces: int = 0
    collector_restarts: int = 0
    transport_errors: int = 0
    partial_records: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rootless_traces": self.rootless_traces,
            "missing_parent_spans": self.missing_parent_spans,
            "multi_service_traces": self.multi_service_traces,
            "duplicate_spans": self.duplicate_spans,
            "conflicting_duplicates": self.conflicting_duplicates,
            "unassigned_traces": self.unassigned_traces,
            "collector_restarts": self.collector_restarts,
            "transport_errors": self.transport_errors,
            "partial_records": self.partial_records,
            "interrupted": self.interrupted,
        }


@dataclass
class TraceWindow:
    """A benchmark window that owns a trace bundle (one Caliper test node)."""

    test_id: str
    start: str  # ISO-8601 UTC
    end: str  # ISO-8601 UTC
    artifact_dir: str
    project: str = ""
    namespace: str = ""
    deployment_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "start": self.start,
            "end": self.end,
            "artifact_dir": self.artifact_dir,
            "project": self.project,
            "namespace": self.namespace,
            "deployment_id": self.deployment_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceWindow:
        required = ("test_id", "start", "end", "artifact_dir")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise TracingConfigError(
                f"trace window missing required field(s): {', '.join(missing)}"
            )
        return cls(
            test_id=str(data["test_id"]),
            start=str(data["start"]),
            end=str(data["end"]),
            artifact_dir=str(data["artifact_dir"]),
            project=str(data.get("project", "")),
            namespace=str(data.get("namespace", "")),
            deployment_id=str(data.get("deployment_id", "")),
        )


@dataclass
class CollectionResult:
    """Outcome of one collection attempt, aggregated across windows."""

    status: CollectionStatus
    reason: str = ""
    bundles: list[str] = field(default_factory=list)
    quality: CollectionQuality = field(default_factory=CollectionQuality)

    @property
    def failed(self) -> bool:
        return self.status is CollectionStatus.FAILED


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TracingConfigError(f"{field_name} must be a boolean, got {type(value).__name__}")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TracingConfigError(f"{field_name} must be an integer, got {type(value).__name__}")
    if value <= 0:
        raise TracingConfigError(f"{field_name} must be positive, got {value}")
    return value


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TracingConfigError(f"{field_name} must be a non-empty string")
    return value


def parse_mode(raw: Any) -> TracingMode:
    """Parse ``tracing.mode`` without coercion; reject unknown values."""
    if not isinstance(raw, str):
        raise TracingConfigError(f"tracing.mode must be a string, got {type(raw).__name__}")
    try:
        return TracingMode(raw.strip().lower())
    except ValueError as exc:
        supported = ", ".join(mode.value for mode in TracingMode)
        raise TracingConfigError(
            f"unsupported tracing.mode '{raw}' (supported: {supported})"
        ) from exc


def parse_sample_ratio(raw: Any) -> float:
    """Parse ``tracing.sample_ratio``; accept only a numeric value in [0, 1]."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TracingConfigError(f"tracing.sample_ratio must be a number, got {type(raw).__name__}")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise TracingConfigError(
            f"tracing.sample_ratio must be between 0 and 1 inclusive, got {value}"
        )
    return value


def settings_from_config(
    get_config: Callable[..., Any],
    *,
    section: str = "tracing",
) -> TracingSettings:
    """Build :class:`TracingSettings` from project configuration.

    ``get_config`` is ``config.project.get_config`` (or any callable with the
    same ``(path, default_value=...)`` contract). Missing keys fall back to
    documented defaults; present keys are validated strictly.

    The default is passed positionally so the contract holds for both the real
    ``get_config(path, default_value=...)`` and lightweight test doubles whose
    second parameter is named ``default``.
    """
    raw_mode = _config_value(get_config, f"{section}.mode", TracingMode.OFF.value)
    mode = parse_mode(raw_mode)
    if mode is TracingMode.OFF:
        # An off configuration must not drag in collector settings that could be
        # malformed; off is always valid and enables nothing.
        return TracingSettings(mode=TracingMode.OFF)

    sample_ratio = parse_sample_ratio(_config_value(get_config, f"{section}.sample_ratio", 0.1))

    def _optional_int(path: str, default: int) -> int:
        raw = _config_value(get_config, path, default)
        return _require_positive_int(raw, path)

    storage_class = _config_value(get_config, f"{section}.collector.storage_class", None)
    if storage_class is not None:
        storage_class = _require_str(storage_class, f"{section}.collector.storage_class")

    return TracingSettings(
        mode=mode,
        sample_ratio=sample_ratio,
        collector_image=_require_str(
            _config_value(get_config, f"{section}.collector.image", DEFAULT_COLLECTOR_IMAGE),
            f"{section}.collector.image",
        ),
        storage_size=_require_str(
            _config_value(get_config, f"{section}.collector.storage_size", DEFAULT_STORAGE_SIZE),
            f"{section}.collector.storage_size",
        ),
        storage_class=storage_class,
        ready_timeout_seconds=_optional_int(
            f"{section}.collector.ready_timeout_seconds", DEFAULT_READY_TIMEOUT_SECONDS
        ),
        settle_timeout_seconds=_optional_int(
            f"{section}.collector.settle_timeout_seconds", DEFAULT_SETTLE_TIMEOUT_SECONDS
        ),
        max_bundle_bytes=_optional_int(
            f"{section}.collector.max_bundle_bytes", DEFAULT_MAX_BUNDLE_BYTES
        ),
    )


def _config_value(get_config: Callable[..., Any], path: str, default: Any) -> Any:
    """Read one config value, passing the default positionally.

    ``config.project.get_config`` accepts the default as its second positional
    argument or as ``default_value=``; test doubles often name it ``default``, so
    passing positionally keeps one call shape working for both.
    """
    return get_config(path, default)
