"""Unit tests for the FORGE toolbox task DSL (decorators + execute_tasks).

Covers: task order, failure → skip log / failure → @always, @when, @retry (falsy success,
falsy exhaustion, exceptions), decorator stack validation, and shared_context return.
See docs/toolbox/dsl.md § Tests.
"""

from __future__ import annotations

import time

import pytest

import projects.core.library.env as env
from projects.core.dsl import always, execute_tasks, retry, task, when
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.dsl.script_manager import reset_script_manager
from projects.core.dsl.task import RetryFailure


def test_tasks_run_in_source_definition_order():
    """Tasks run in source definition order when all succeed (registration order)."""
    reset_script_manager()
    events: list[str] = []

    @task
    def first(args, ctx):
        events.append("first")

    @task
    def second(args, ctx):
        events.append("second")

    execute_tasks(locals())
    assert events == ["first", "second"]


def test_skip_pending_emits_skip_banner(monkeypatch):
    """After a task fails, later non-@always tasks are skipped and the skip is logged to task.log.

    Asserts the log names the skipped task and records that it was skipped because it is not @always.
    """
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)

    @task
    def a1(args, ctx):
        return True

    @task
    def a2(args, ctx):
        raise RuntimeError("stop")

    @task
    def a3_pending_skip(args, ctx):
        return "TASK_A3_RETURN_VALUE"

    @task
    def a4(args, ctx):
        return "TASK_A4_RETURN_VALUE"

    fn_locals = {"a1": a1, "a2": a2, "a3_pending_skip": a3_pending_skip, "a4": a4}
    with pytest.raises(TaskExecutionError):
        execute_tasks(fn_locals)

    # After execute_tasks, env.ARTIFACT_DIR is restored to the parent; task.log lives under */*__*/.
    candidates = list(env.ARTIFACT_DIR.glob("*__*/task.log"))
    assert candidates, "expected nested artifact task.log"
    task_log = max(candidates, key=lambda p: p.stat().st_mtime)
    text = task_log.read_text(encoding="utf-8")
    assert "SKIPPING TASK: a3_pending_skip" in text
    assert "not @always" in text
    # Bodies of pending non-@always tasks must not run (no logged return lines for these markers).
    assert "TASK_A3_RETURN_VALUE" not in text
    assert "TASK_A4_RETURN_VALUE" not in text


def test_failure_skips_pending_but_runs_always(monkeypatch):
    """On mid-pipeline failure: pending normal tasks do not run; @always tasks still run.

    Asserts event order and that the raised TaskExecutionError chains the original RuntimeError
    via __cause__ (per runtime: raise TaskExecutionError from e).
    """
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    events: list[str] = []

    @task
    def a1(args, ctx):
        events.append("a1")
        return True

    @task
    def a2(args, ctx):
        events.append("a2")
        raise RuntimeError("stop")

    @task
    def a3(args, ctx):
        events.append("a3")

    @always
    @task
    def a4(args, ctx):
        events.append("a4")
        return "TASK_A4_ALWAYS_RETURN_VALUE"

    with pytest.raises(TaskExecutionError) as ei:
        execute_tasks(locals())

    assert isinstance(ei.value.__cause__, RuntimeError)
    assert str(ei.value.__cause__) == "stop"
    assert events == ["a1", "a2", "a4"]

    # After execute_tasks, find nested task-always.log and prove @always body ran (return logged).
    # Since this is a failure scenario, @always task logs are split into task-always.log
    always_candidates = list(env.ARTIFACT_DIR.glob("*__*/task-always.log"))
    assert always_candidates, "expected nested artifact task-always.log"
    always_log = max(always_candidates, key=lambda p: p.stat().st_mtime)
    assert "TASK_A4_ALWAYS_RETURN_VALUE" in always_log.read_text(encoding="utf-8")


def test_when_skips_task():
    """When the @when predicate is false, the task body is not executed."""
    reset_script_manager()
    flag = {"run": False}

    @when(lambda: False)
    @task
    def skipped(args, ctx):
        flag["run"] = True
        return True

    execute_tasks(locals())
    assert flag["run"] is False


def test_retry_falsy_then_truthy(monkeypatch):
    """@retry re-runs a task when it returns falsy until it returns truthy or attempts are exhausted."""
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    n = {"i": 0}

    @retry(attempts=4, delay=0, backoff=1.0)
    @task
    def poll(args, ctx):
        n["i"] += 1
        return n["i"] >= 3

    execute_tasks(locals())
    assert n["i"] == 3


def test_retry_falsy_exhausted(monkeypatch):
    """If every attempt returns falsy, retry ends with RetryFailure as TaskExecutionError.__cause__."""
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)

    @retry(attempts=2, delay=0, backoff=1.0)
    @task
    def never_truthy(args, ctx):
        return False

    with pytest.raises(TaskExecutionError) as ei:
        execute_tasks(locals())
    assert isinstance(ei.value.__cause__, RetryFailure)


def test_retry_on_exceptions(monkeypatch):
    """With retry_on_exceptions=True, transient exceptions are retried until success."""
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    n = {"i": 0}

    @retry(attempts=4, delay=0, backoff=1.0, retry_on_exceptions=True)
    @task
    def flaky(args, ctx):
        n["i"] += 1
        if n["i"] < 3:
            raise ValueError("transient")
        return "ok"

    execute_tasks(locals())
    assert n["i"] == 3


def test_retry_on_exceptions_exhausted(monkeypatch):
    """When every attempt raises, execute_tasks fails with TaskExecutionError chained from RetryFailure."""
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)

    @retry(attempts=2, delay=0, backoff=1.0, retry_on_exceptions=True)
    @task
    def always_bad(args, ctx):
        raise ValueError("nope")

    with pytest.raises(TaskExecutionError) as ei:
        execute_tasks(locals())
    assert isinstance(ei.value.__cause__, RetryFailure)


def test_retry_decorator_requires_task():
    """@retry without @task raises TypeError at function definition time (decorator order).

    Expects the full multi-line message: only @task functions may be wrapped; @task must be
    below @retry. The function body is never executed—pytest.raises asserts the definition fails.
    """
    _RETRY_REQUIRES_TASK_RE = (
        r"@retry can only be applied to functions decorated with @task\.\s+"
        r"Function 'not_decorated' is not a task\.\s+"
        r"Put '@task' BELOW '@retry' in your decorator stack\."
    )
    with pytest.raises(TypeError, match=_RETRY_REQUIRES_TASK_RE):

        @retry(attempts=2, delay=0)
        def not_decorated():
            pass


def test_when_decorator_requires_task():
    """@when without @task raises TypeError at definition time, same contract as test_retry_decorator_requires_task."""
    _WHEN_REQUIRES_TASK_RE = (
        r"@when can only be applied to functions decorated with @task\.\s+"
        r"Function 'not_decorated' is not a task\.\s+"
        r"Put '@task' BELOW '@when' in your decorator stack\."
    )
    with pytest.raises(TypeError, match=_WHEN_REQUIRES_TASK_RE):

        @when(lambda: True)
        def not_decorated():
            pass


def test_execute_tasks_success_returns_shared_context():
    """On success, execute_tasks returns shared_context with task mutations and artifact_dir set."""
    reset_script_manager()
    ctx_marker = object()

    @task
    def only(args, ctx):
        ctx.marker = ctx_marker
        return "done"

    out = execute_tasks(locals())
    assert out.marker is ctx_marker
    assert getattr(out, "artifact_dir", None) is not None
