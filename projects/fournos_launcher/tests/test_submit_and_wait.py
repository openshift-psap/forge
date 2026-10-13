"""Unit tests for submit_and_wait toolbox (Fournos job launch/wait).

Regression coverage for Fournos integration used by RHAIIS CPU CI pipelines.
Does not introduce submit_and_wait behavior — it guards existing EarlyReturn,
status polling, and retry configuration.
"""

from __future__ import annotations

import types

import pytest

from projects.core.dsl import shell
from projects.core.dsl.control_flow import EarlyReturn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(stdout="", stderr="", returncode=0, command="oc"):
    return shell.CommandResult(stdout=stdout, stderr=stderr, returncode=returncode, command=command)


def _args(**kwargs):
    return types.SimpleNamespace(**kwargs)


def _ctx(**kwargs):
    return types.SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# wait_for_job_to_resolve – status polling logic
#
# Tasks are called directly (not via execute_tasks) because execute_tasks
# resolves tasks from the script-manager registry keyed by the *caller's*
# file, not from the dict passed to it.  Calling the @task wrapper directly
# exercises the same function body without the DSL pipeline machinery.
# ---------------------------------------------------------------------------


def test_resolve_returns_truthy_on_pending(monkeypatch):
    """wait_for_job_to_resolve returns truthy when job reaches Pending status."""
    monkeypatch.setattr(shell, "run", lambda *a, **kw: _make_result(stdout="Pending"))

    from projects.fournos_launcher.toolbox.submit_and_wait.main import wait_for_job_to_resolve

    result = wait_for_job_to_resolve(
        _args(namespace="fournos-jobs"), _ctx(final_job_name="test-job")
    )
    assert result


def test_resolve_raises_on_not_found(monkeypatch):
    """wait_for_job_to_resolve raises FournosJobFailureError immediately when job is not found."""
    monkeypatch.setattr(
        shell, "run", lambda *a, **kw: _make_result(stdout="", stderr="not found", returncode=1)
    )

    from projects.fournos_launcher.toolbox.submit_and_wait.main import (
        FournosJobFailureError,
        wait_for_job_to_resolve,
    )

    with pytest.raises(FournosJobFailureError):
        wait_for_job_to_resolve(_args(namespace="fournos-jobs"), _ctx(final_job_name="test-job"))


def test_resolve_raises_on_stopping(monkeypatch):
    """wait_for_job_to_resolve raises FournosJobFailureError when job enters Stopping."""
    monkeypatch.setattr(shell, "run", lambda *a, **kw: _make_result(stdout="Stopping"))

    from projects.fournos_launcher.toolbox.submit_and_wait.main import (
        FournosJobFailureError,
        wait_for_job_to_resolve,
    )

    with pytest.raises(FournosJobFailureError):
        wait_for_job_to_resolve(_args(namespace="fournos-jobs"), _ctx(final_job_name="test-job"))


def test_resolve_succeeds_immediately_when_already_running(monkeypatch):
    """If job is already Running/Admitted/Succeeded, wait_for_job_to_resolve returns immediately."""
    from projects.fournos_launcher.toolbox.submit_and_wait.main import wait_for_job_to_resolve

    for terminal_status in ["Running", "Admitted", "Succeeded"]:
        monkeypatch.setattr(
            shell, "run", lambda *a, status=terminal_status, **kw: _make_result(stdout=status)
        )
        result = wait_for_job_to_resolve(
            _args(namespace="fournos-jobs"), _ctx(final_job_name="test-job")
        )
        assert result, f"Expected truthy result for status={terminal_status}"


# ---------------------------------------------------------------------------
# wait_for_job_completion – delay changed from 10s to 30s (doc check)
# ---------------------------------------------------------------------------


def test_wait_for_job_completion_retry_config():
    """wait_for_job_completion should have 3000 attempts and 30s delay (not 10s)."""
    from projects.fournos_launcher.toolbox.submit_and_wait import main as m

    retry_cfg = m.wait_for_job_completion._retry_config
    assert retry_cfg["delay"] == 30
    assert retry_cfg["attempts"] == 3000


# ---------------------------------------------------------------------------
# check_early_return – passthrough when wait=True
# ---------------------------------------------------------------------------


def test_check_early_return_noop_when_wait_true():
    """check_early_return returns a plain string (no EarlyReturn) when wait=True."""
    from projects.fournos_launcher.toolbox.submit_and_wait.main import check_early_return

    result = check_early_return(_args(wait=True), _ctx(final_job_name="test-job"))
    assert result
    assert not isinstance(result, EarlyReturn)
