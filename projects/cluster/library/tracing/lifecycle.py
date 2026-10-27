"""Capture lifecycle composition for project orchestration.

Resolves tracing settings, deploys the collector, persists benchmark windows,
captures traces, and reconciles before cleanup. Only orchestration calls this:
it reads project configuration and hands explicit typed inputs to the toolbox
commands. Shared helpers and toolbox commands never import project
orchestration.

Recovery: capture identity and benchmark windows are persisted as soon as they
are known so a later finalization phase can recover data after an interruption.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    CollectionStatus,
    TraceWindow,
    TracingSettings,
    settings_from_config,
)
from projects.cluster.toolbox.capture_traces.main import run as capture_traces
from projects.cluster.toolbox.cleanup_trace_collector.main import (
    run as cleanup_trace_collector,
)
from projects.cluster.toolbox.deploy_trace_collector.main import (
    run as deploy_trace_collector,
)
from projects.core.library import env

logger = logging.getLogger(__name__)

WINDOWS_FILENAME = "trace-windows.json"
CAPTURE_STATE_FILENAME = "trace-capture-state.json"


class TracingLifecycleError(RuntimeError):
    """Raised when trace collection cannot complete as requested."""


@dataclass
class CaptureSession:
    """State for one deployment capture, shared across sequential workloads."""

    identity: CaptureIdentity
    settings: TracingSettings
    output_root: Path = field(default_factory=Path)
    roles: list[str] = field(default_factory=list)
    windows: list[TraceWindow] = field(default_factory=list)

    def add_window(self, window: TraceWindow) -> None:
        self.windows.append(window)
        self.persist_windows()

    def _windows_path(self) -> Path:
        return self.output_root / WINDOWS_FILENAME

    def persist_windows(self) -> None:
        """Persist benchmark windows so finalization can recover after a crash."""
        payload = [w.to_dict() for w in self.windows]
        self._windows_path().parent.mkdir(parents=True, exist_ok=True)
        self._windows_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def windows_from_disk(self) -> list[TraceWindow]:
        path = self._windows_path()
        if not path.is_file():
            return list(self.windows)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [TraceWindow.from_dict(entry) for entry in raw]


def resolve_settings() -> TracingSettings:
    """Resolve tracing settings from project configuration."""
    from projects.core.library import config

    return settings_from_config(config.project.get_config)


def start_capture(
    *,
    job_id: str,
    capture_id: str,
    project: str,
    namespace: str,
    deployment_id: str,
    output_root: Path | None = None,
    settings: TracingSettings | None = None,
    roles: list[str] | None = None,
) -> CaptureSession:
    """Deploy the collector and return a capture session.

    Returns ``None`` semantics via :class:`TracingLifecycleError` only when
    tracing is enabled but cannot start; callers must handle a disabled capture
    by not creating a session at all.
    """
    settings = settings or resolve_settings()
    if not settings.enabled:
        raise TracingLifecycleError("start_capture called with tracing disabled")

    identity = CaptureIdentity(
        job_id=job_id,
        capture_id=capture_id,
        project=project,
        namespace=namespace,
        deployment_id=deployment_id,
    )
    output_root = output_root or env.ARTIFACT_DIR

    _deploy_collector(identity, settings)

    session = CaptureSession(
        identity=identity,
        settings=settings,
        output_root=Path(output_root),
        roles=list(roles or []),
    )
    _persist_capture_state(session)
    return session


def _deploy_collector(identity: CaptureIdentity, settings: TracingSettings) -> None:
    """Deploy and verify the collector through the owning toolbox command."""
    deploy_trace_collector(
        namespace=identity.namespace,
        capture_id=identity.capture_id,
        job_id=identity.job_id,
        project=identity.project,
        deployment_id=identity.deployment_id,
        mode=settings.mode.value,
        sample_ratio=settings.sample_ratio,
        collector_image=settings.collector_image,
        storage_size=settings.storage_size,
        storage_class=settings.storage_class,
        ready_timeout_seconds=settings.ready_timeout_seconds,
    )


def _state_path(output_root: Path) -> Path:
    return output_root / CAPTURE_STATE_FILENAME


def _persist_capture_state(session: CaptureSession) -> None:
    state = {
        "capture_id": session.identity.capture_id,
        "roles": session.roles,
        "job_id": session.identity.job_id,
        "project": session.identity.project,
        "namespace": session.identity.namespace,
        "deployment_id": session.identity.deployment_id,
        "mode": session.settings.mode.value,
        "sample_ratio": session.settings.sample_ratio,
        "collector_image": session.settings.collector_image,
        "created_at": datetime.now(UTC).isoformat(),
    }
    path = _state_path(session.output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_window(
    session: CaptureSession,
    *,
    test_id: str,
    start: datetime,
    end: datetime,
    artifact_dir: Path,
) -> TraceWindow:
    """Record a benchmark window for a test, in UTC."""
    window = TraceWindow(
        test_id=test_id,
        start=_utc_iso(start),
        end=_utc_iso(end),
        artifact_dir=str(artifact_dir),
        project=session.identity.project,
        namespace=session.identity.namespace,
        deployment_id=session.identity.deployment_id,
    )
    session.add_window(window)
    return window


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def capture(session: CaptureSession, *, interrupted: bool = False) -> CollectionStatus:
    """Snapshot, transfer, sanitize, and publish bundles for every window."""
    windows = session.windows_from_disk()
    if not windows:
        logger.info("No trace windows recorded; nothing to capture")
        return CollectionStatus.EMPTY

    capture_traces(
        namespace=session.identity.namespace,
        capture_id=session.identity.capture_id,
        windows_file=str(session.output_root / WINDOWS_FILENAME),
        output_dir=str(session.output_root),
        project=session.identity.project,
        job_id=session.identity.job_id,
        deployment_id=session.identity.deployment_id,
        mode=session.settings.mode.value,
        sample_ratio=session.settings.sample_ratio,
        collector_image=session.settings.collector_image,
        instrumentation_roles=",".join(session.roles),
        settle_timeout_seconds=session.settings.settle_timeout_seconds,
        max_bundle_bytes=session.settings.max_bundle_bytes,
        interrupted=interrupted,
    )

    result_path = session.output_root / "trace-capture-result.json"
    if not result_path.is_file():
        raise TracingLifecycleError("capture did not produce a trace-capture-result.json")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    status = result.get("status", "failed")
    try:
        return CollectionStatus(status)
    except ValueError as exc:
        raise TracingLifecycleError(f"invalid capture status '{status}'") from exc


def cleanup(session: CaptureSession, *, force: bool = False) -> None:
    """Remove the collector and spool after a verified capture."""
    result_path = session.output_root / "trace-capture-result.json"
    cleanup_trace_collector(
        namespace=session.identity.namespace,
        capture_id=session.identity.capture_id,
        job_id=session.identity.job_id,
        project=session.identity.project,
        deployment_id=session.identity.deployment_id,
        capture_result_file=str(result_path) if result_path.is_file() else None,
        force=force,
    )
