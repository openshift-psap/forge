from types import SimpleNamespace
from unittest.mock import patch

import pytest

from projects.rhaiis.toolbox.copy_profiler_traces import main as copy_traces_mod


class FakeCommandResult:
    """Minimal stand-in for shell.CommandResult."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_args(flush_timeout=120, flush_poll_interval=10, namespace="test-ns"):
    return SimpleNamespace(
        flush_timeout=flush_timeout,
        flush_poll_interval=flush_poll_interval,
        namespace=namespace,
    )


def _make_context(pod_name="pod/predictor-0"):
    return SimpleNamespace(pod_name=pod_name)


def test_list_trace_files_succeeds_immediately():
    """Traces are present on the first attempt — no retry needed."""
    args = _make_args()
    context = _make_context()

    fake_result = FakeCommandResult(stdout="/tmp/trace_rank0_a.json\n/tmp/trace_rank0_b.json\n")

    with (
        patch.object(copy_traces_mod.shell, "run", return_value=fake_result),
        patch.object(copy_traces_mod.time, "monotonic", return_value=1000.0),
    ):
        msg = copy_traces_mod.list_trace_files(args, context)

    assert context.trace_count == 2
    assert "Found 2" in msg


def test_list_trace_files_retries_then_succeeds():
    """Traces not present on first attempts, appear on a later attempt."""
    args = _make_args(flush_timeout=30, flush_poll_interval=10)
    context = _make_context()

    missing = FakeCommandResult(stdout="NO_RANK0_TRACES\n")
    found = FakeCommandResult(stdout="/tmp/trace_rank0_pid1.json\n")

    call_count = 0

    def fake_shell_run(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return missing
        return found

    # monotonic() calls: deadline setup (1000.0), deadline check iter1 (1005.0),
    # deadline check iter2 (1015.0). Iter3 finds traces before deadline check.
    monotonic_values = iter([1000.0, 1005.0, 1015.0])

    with (
        patch.object(copy_traces_mod.shell, "run", side_effect=fake_shell_run),
        patch.object(copy_traces_mod.time, "sleep") as mock_sleep,
        patch.object(copy_traces_mod.time, "monotonic", side_effect=monotonic_values),
    ):
        msg = copy_traces_mod.list_trace_files(args, context)

    assert context.trace_count == 1
    assert "Found 1" in msg
    # Two sleeps before the third (successful) attempt
    assert mock_sleep.call_count == 2


def test_list_trace_files_raises_after_timeout():
    """Traces never appear — should raise after the elapsed-time deadline."""
    args = _make_args(flush_timeout=20, flush_poll_interval=10)
    context = _make_context()

    missing = FakeCommandResult(stdout="NO_RANK0_TRACES\n")

    # monotonic() calls: deadline setup (1000.0 → deadline=1020.0),
    # check iter1 (1005.0 < 1020 → sleep), check iter2 (1015.0 < 1020 → sleep),
    # check iter3 (1025.0 >= 1020 → raise).
    monotonic_values = iter([1000.0, 1005.0, 1015.0, 1025.0])

    with (
        patch.object(copy_traces_mod.shell, "run", return_value=missing),
        patch.object(copy_traces_mod.time, "sleep"),
        patch.object(copy_traces_mod.time, "monotonic", side_effect=monotonic_values),
    ):
        with pytest.raises(RuntimeError, match="No rank-0 profiler traces found"):
            copy_traces_mod.list_trace_files(args, context)


def test_list_trace_files_empty_stdout_treated_as_missing():
    """An empty stdout (no files and no sentinel) is treated as missing."""
    args = _make_args(flush_timeout=10, flush_poll_interval=10)
    context = _make_context()

    empty = FakeCommandResult(stdout="")

    # monotonic() calls: deadline setup (1000.0 → deadline=1010.0),
    # check iter1 (1015.0 >= 1010 → raise immediately).
    monotonic_values = iter([1000.0, 1015.0])

    with (
        patch.object(copy_traces_mod.shell, "run", return_value=empty),
        patch.object(copy_traces_mod.time, "sleep"),
        patch.object(copy_traces_mod.time, "monotonic", side_effect=monotonic_values),
    ):
        with pytest.raises(RuntimeError, match="No rank-0 profiler traces found"):
            copy_traces_mod.list_trace_files(args, context)


def test_list_trace_files_rejects_zero_poll_interval():
    """flush_poll_interval=0 should raise ValueError before any polling."""
    args = _make_args(flush_poll_interval=0)
    context = _make_context()

    with pytest.raises(ValueError, match="flush_poll_interval must be a positive integer"):
        copy_traces_mod.list_trace_files(args, context)


def test_list_trace_files_rejects_negative_poll_interval():
    """flush_poll_interval=-5 should raise ValueError before any polling."""
    args = _make_args(flush_poll_interval=-5)
    context = _make_context()

    with pytest.raises(ValueError, match="flush_poll_interval must be a positive integer, got -5"):
        copy_traces_mod.list_trace_files(args, context)


def test_list_trace_files_found_near_deadline():
    """Traces arriving just before the deadline should still succeed."""
    args = _make_args(flush_timeout=30, flush_poll_interval=10)
    context = _make_context()

    missing = FakeCommandResult(stdout="NO_RANK0_TRACES\n")
    found = FakeCommandResult(stdout="/tmp/trace_rank0_late.json\n")

    call_count = 0

    def fake_shell_run(*a, **kw):
        nonlocal call_count
        call_count += 1
        # Traces appear on the third attempt, just before deadline
        if call_count < 3:
            return missing
        return found

    # monotonic() calls: deadline setup (1000.0 → deadline=1030.0),
    # check iter1 (1010.0 < 1030 → sleep), check iter2 (1028.0 < 1030 → sleep),
    # iter3 finds traces before deadline check.
    monotonic_values = iter([1000.0, 1010.0, 1028.0])

    with (
        patch.object(copy_traces_mod.shell, "run", side_effect=fake_shell_run),
        patch.object(copy_traces_mod.time, "sleep") as mock_sleep,
        patch.object(copy_traces_mod.time, "monotonic", side_effect=monotonic_values),
    ):
        msg = copy_traces_mod.list_trace_files(args, context)

    assert context.trace_count == 1
    assert "Found 1" in msg
    assert mock_sleep.call_count == 2
    # iter2: remaining = 1030.0 - 1028.0 = 2.0, which is < poll_interval (10).
    # The sleep must be capped to the remaining time.
    mock_sleep.assert_called_with(2.0)
