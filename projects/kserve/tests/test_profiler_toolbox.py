"""Regression tests for the profiler toolbox commands.

Guard against uninterpolated f-string placeholders (e.g. a plain string
containing "{args.namespace}") reaching the cluster: a literal placeholder
as a namespace/selector silently queries a nonexistent object and produces
misleading "not found" failures.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from projects.core.dsl import shell as dsl_shell
from projects.kserve.toolbox.copy_profiler_traces import main as copy_traces
from projects.kserve.toolbox.enable_profiler_gate import main as profiler_gate
from projects.kserve.toolbox.verify_profiler_prereqs import main as verify_prereqs

TOOLBOX_MODULES = [
    verify_prereqs,
    profiler_gate,
    copy_traces,
]


def test_no_uninterpolated_placeholders_in_toolbox_commands() -> None:
    """No plain string literal may contain an {args.*}/{context.*} placeholder.

    Inside real f-strings, interpolations are FormattedValue nodes, so any
    string Constant holding a placeholder is a missing ``f`` prefix.
    """
    for module in TOOLBOX_MODULES:
        source = Path(module.__file__).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "{args." not in node.value, (
                    f"{module.__name__}:{node.lineno} has an uninterpolated placeholder: {node.value!r}"
                )
                assert "{context." not in node.value, (
                    f"{module.__name__}:{node.lineno} has an uninterpolated placeholder: {node.value!r}"
                )


def _run_with_captured_commands(task, args, context, stdout="Running"):
    captured = []

    def fake_run(cmd, check=True, **_kwargs):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout=stdout)

    with patch.object(dsl_shell, "run", side_effect=fake_run):
        result = task(args, context)
    return result, captured


def test_check_webhook_pod_interpolates_the_webhook_namespace() -> None:
    args = SimpleNamespace(namespace="ns-target", webhook_namespace="ns-webhook")
    result, captured = _run_with_captured_commands(
        verify_prereqs.check_webhook_pod, args, SimpleNamespace()
    )

    assert result == "OK: webhook pod is Running"
    assert len(captured) == 1
    assert "oc get pods -n ns-webhook -l app=env-injector" in captured[0]
    assert "{args" not in captured[0]


def test_gate_find_predictor_pod_interpolates_the_selector() -> None:
    args = SimpleNamespace(
        pod_selector="app.kubernetes.io/name=llm-d-simple-tp1-x1",
        namespace="ns-target",
        container="main",
    )
    result, captured = _run_with_captured_commands(
        profiler_gate.find_predictor_pod, args, SimpleNamespace(), stdout="pod/the-pod"
    )

    assert result == "Found pod: pod/the-pod"
    assert "-lapp.kubernetes.io/name=llm-d-simple-tp1-x1" in captured[0]
    assert "-n ns-target" in captured[0]
    assert "{args" not in captured[0]


def test_gate_toggle_interpolates_the_container() -> None:
    context = SimpleNamespace(pod_name="pod/the-pod")
    args = SimpleNamespace(
        pod_selector="ignored-by-toggle",
        namespace="ns-target",
        container="main",
        gate_value="isl1000_osl1000",
        disable=False,
        clear_traces=True,
    )
    _result, captured = _run_with_captured_commands(
        profiler_gate.toggle_profiler_gate, args, context
    )

    assert len(captured) == 2  # clear traces, then enable the gate
    assert all("-c main" in cmd for cmd in captured)
    assert all("-n ns-target" in cmd for cmd in captured)
    assert "echo isl1000_osl1000 > /tmp/profiler_gate" in captured[1]
    assert all("{args" not in cmd for cmd in captured)


def test_copy_find_predictor_pod_interpolates_the_selector() -> None:
    args = SimpleNamespace(
        pod_selector="serving.kserve.io/inferenceservice=model",
        namespace="ns-target",
        container="kserve-container",
    )
    result, captured = _run_with_captured_commands(
        copy_traces.find_predictor_pod, args, SimpleNamespace(), stdout="pod/the-pod"
    )

    assert result == "Found pod: pod/the-pod"
    assert "-lserving.kserve.io/inferenceservice=model" in captured[0]
    assert "{args" not in captured[0]
