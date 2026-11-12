"""Tests for the ag_praxis tracing capture lifecycle wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from projects.cluster.library.tracing.lifecycle import CaptureSession
from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    CollectionStatus,
    TracingMode,
    TracingSettings,
)


def _settings(sample_ratio: float = 1.0) -> TracingSettings:
    return TracingSettings(mode=TracingMode.STANDARD, sample_ratio=sample_ratio)


def _session(sample_ratio: float = 1.0, windows: list | None = None) -> CaptureSession:
    return CaptureSession(
        identity=CaptureIdentity(
            job_id="job",
            capture_id="cap",
            project="ag_praxis",
            namespace="test-ns",
            deployment_id="sim",
        ),
        settings=_settings(sample_ratio),
        output_root=Path("/unused"),
        roles=[],
        windows=windows or [],
    )


def test_start_trace_capture_disabled_returns_none(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase, tracing_adapter
    from projects.cluster.library.tracing import lifecycle

    monkeypatch.setattr(
        tracing_adapter, "resolve_tracing", lambda: TracingSettings(mode=TracingMode.OFF)
    )
    called = []
    monkeypatch.setattr(lifecycle, "start_capture", lambda **kwargs: called.append(kwargs))

    assert test_phase._start_trace_capture("sim") is None
    assert called == []


def test_start_trace_capture_enabled_starts_collector(monkeypatch, tmp_path) -> None:
    from projects.ag_praxis.orchestration import test_phase, tracing_adapter
    from projects.cluster.library.tracing import lifecycle

    monkeypatch.setattr(
        "projects.core.library.config.project",
        SimpleNamespace(
            get_config=lambda key, default=None: {
                "ci_job.fjob_name": "fjob-42",
                "test.namespace": "test-ns",
            }.get(key, default)
        ),
    )
    monkeypatch.setattr(tracing_adapter, "resolve_tracing", lambda: _settings(sample_ratio=0.5))
    monkeypatch.setattr("projects.core.library.env.ARTIFACT_DIR", tmp_path)

    captured = {}

    def fake_start_capture(**kwargs):
        captured.update(kwargs)
        return "session"

    monkeypatch.setattr(lifecycle, "start_capture", fake_start_capture)

    assert test_phase._start_trace_capture("sim") == "session"
    assert captured["project"] == "ag_praxis"
    assert captured["namespace"] == "test-ns"
    assert captured["deployment_id"] == "sim"
    assert captured["job_id"] == "fjob-42"
    assert captured["roles"] == []
    assert captured["output_root"] == tmp_path
    assert captured["settings"].sample_ratio == 0.5


def test_record_trace_window_skipped_without_session_or_benchmark(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    called = []
    monkeypatch.setattr(lifecycle, "record_window", lambda *args, **kwargs: called.append(args))

    start, end = datetime.now(UTC), datetime.now(UTC)

    test_phase._record_trace_window(None, True, start, end)
    test_phase._record_trace_window(_session(), False, start, end)
    assert called == []

    session = _session()
    monkeypatch.setattr("projects.core.library.env.ARTIFACT_DIR", Path("/artifacts/svc"))
    test_phase._record_trace_window(session, True, start, end)

    assert len(called) == 1
    assert called[0][0] is session


def test_record_trace_window_uses_endpoint_artifact_dir(monkeypatch, tmp_path) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    captured = {}
    monkeypatch.setattr(
        lifecycle,
        "record_window",
        lambda session, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr("projects.core.library.env.ARTIFACT_DIR", tmp_path)

    start, end = datetime.now(UTC), datetime.now(UTC)
    test_phase._record_trace_window(_session(), True, start, end)

    assert captured["test_id"] == tmp_path.name
    assert captured["artifact_dir"] == tmp_path
    assert captured["start"] is start
    assert captured["end"] is end


def test_capture_traces_noop_without_session(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    called = []
    monkeypatch.setattr(lifecycle, "cleanup", lambda session: called.append(session))

    test_phase._capture_traces(None)
    assert called == []


def test_capture_traces_no_windows_cleans_up_only(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    session = _session()
    cleaned = []
    monkeypatch.setattr(lifecycle, "cleanup", lambda s: cleaned.append(s))
    captured = []
    monkeypatch.setattr(
        lifecycle, "capture", lambda s: captured.append(s) or CollectionStatus.EMPTY
    )

    test_phase._capture_traces(session)

    assert captured == []
    assert cleaned == [session]


def test_capture_traces_failure_notifies_cleans_up_and_raises(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    session = _session(windows=[object()])
    cleaned = []
    monkeypatch.setattr(lifecycle, "cleanup", lambda s: cleaned.append(s))

    def failing_capture(s):
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "capture", failing_capture)

    notifications = []
    monkeypatch.setattr(
        "projects.core.library.ci.add_notification_file",
        lambda name, message: notifications.append(name),
    )

    with pytest.raises(RuntimeError, match="requested trace collection failed"):
        test_phase._capture_traces(session)

    assert notifications == ["tracing-capture-failed"]
    assert cleaned == [session]


def test_capture_traces_empty_at_full_sampling_fails(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    session = _session(sample_ratio=1.0, windows=[object()])
    monkeypatch.setattr(lifecycle, "cleanup", lambda s: None)
    monkeypatch.setattr(lifecycle, "capture", lambda s: CollectionStatus.EMPTY)

    notifications = []
    monkeypatch.setattr(
        "projects.core.library.ci.add_notification_file",
        lambda name, message: notifications.append(name),
    )

    with pytest.raises(RuntimeError, match="empty-at-full-sampling"):
        test_phase._capture_traces(session)

    assert notifications == ["tracing-collection-empty-at-full-sampling"]


def test_capture_traces_empty_at_low_sampling_passes(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    session = _session(sample_ratio=0.1, windows=[object()])
    monkeypatch.setattr(lifecycle, "cleanup", lambda s: None)
    monkeypatch.setattr(lifecycle, "capture", lambda s: CollectionStatus.EMPTY)

    test_phase._capture_traces(session)


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_capture_traces_bad_status_fails(monkeypatch, status) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.cluster.library.tracing import lifecycle

    session = _session(sample_ratio=0.1, windows=[object()])
    monkeypatch.setattr(lifecycle, "cleanup", lambda s: None)
    monkeypatch.setattr(lifecycle, "capture", lambda s: CollectionStatus(status))

    notifications = []
    monkeypatch.setattr(
        "projects.core.library.ci.add_notification_file",
        lambda name, message: notifications.append(name),
    )

    with pytest.raises(RuntimeError, match=f"requested trace collection {status}"):
        test_phase._capture_traces(session)

    assert notifications == [f"tracing-collection-{status}"]


def _patch_cleanup_finalizer(monkeypatch, order, session):
    monkeypatch.setattr(
        "projects.core.library.config.project",
        SimpleNamespace(
            get_config=lambda key, default=None: {"test.skip_cleanup": True}.get(key, default)
        ),
    )
    monkeypatch.setattr(
        "projects.kserve.toolbox.capture_llmisvc_state.main.run",
        lambda llmisvc_name, namespace: order.append("llmisvc-state"),
    )
    monkeypatch.setattr(
        "projects.cluster.library.tracing.lifecycle.cleanup", lambda s: order.append("traces")
    )
    monkeypatch.setattr(
        "projects.cluster.library.tracing.lifecycle.capture",
        lambda s: CollectionStatus.EMPTY,
    )
    monkeypatch.setattr(
        "projects.core.library.ci.add_notification_file", lambda name, message: None
    )


def test_run_cleanup_reports_trace_capture_failure(monkeypatch) -> None:
    """A trace capture failure is returned as the finalizer exception."""
    from projects.ag_praxis.orchestration import test_phase

    order = []
    session = _session(windows=[object()])
    _patch_cleanup_finalizer(monkeypatch, order, session)

    finalizer_exc = test_phase._run_cleanup(
        "test-ns", {"llmisvc_name": "sim"}, None, capture_session=session
    )

    assert order == ["llmisvc-state", "traces"]
    assert finalizer_exc is not None
    assert "empty-at-full-sampling" in str(finalizer_exc[1])


def test_run_cleanup_trace_failure_ignored_after_primary_failure(monkeypatch) -> None:
    """The trace capture finalizer still runs when the primary test failed.

    Its failure is logged and dropped: the primary exception is raised anyway
    and must not be masked by a finalizer failure.
    """
    from projects.ag_praxis.orchestration import test_phase

    order = []
    session = _session(windows=[object()])
    _patch_cleanup_finalizer(monkeypatch, order, session)

    primary_exc = (RuntimeError, RuntimeError("primary"), None)
    finalizer_exc = test_phase._run_cleanup(
        "test-ns", {"llmisvc_name": "sim"}, primary_exc, capture_session=session
    )

    assert order == ["llmisvc-state", "traces"]
    assert finalizer_exc is None


def test_run_cleanup_no_session(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase

    monkeypatch.setattr(
        "projects.core.library.config.project",
        SimpleNamespace(
            get_config=lambda key, default=None: {"test.skip_cleanup": True}.get(key, default)
        ),
    )
    monkeypatch.setattr(
        "projects.kserve.toolbox.capture_llmisvc_state.main.run",
        lambda llmisvc_name, namespace: None,
    )

    finalizer_exc = test_phase._run_cleanup("test-ns", {"llmisvc_name": "sim"}, None)
    assert finalizer_exc is None
