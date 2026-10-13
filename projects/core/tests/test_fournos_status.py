import json
import shlex
from types import SimpleNamespace

import pytest

from projects.core.library import fournos_status
from projects.core.library.fournos_status import relevant_deployments_status_patch


def _enable_fournos(monkeypatch) -> None:
    monkeypatch.setattr(fournos_status.env, "running_inside_fournos", lambda: True)


def test_relevant_deployment_status_patch_sets_reference() -> None:
    reference = {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "name": "llama-3-70b-abc123",
        "namespace": "rhaiis",
        "runUUID": "run-123",
    }

    assert relevant_deployments_status_patch(reference) == {
        "status": {"engineStatus": {"forge": {"relevantDeployments": [reference]}}}
    }


def test_relevant_deployment_status_patch_clears_only_its_leaf() -> None:
    assert relevant_deployments_status_patch(None) == {
        "status": {"engineStatus": {"forge": {"relevantDeployments": None}}}
    }


def test_patch_fjob_status_uses_status_subresource_and_management_context(monkeypatch) -> None:
    _enable_fournos(monkeypatch)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setenv("KUBECONFIG", "/tmp/target-cluster-config")
    monkeypatch.setattr(fournos_status.run, "run", fake_run)

    reference = {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "name": "isvc-1",
        "namespace": "rhaiis",
        "runUUID": "run-1",
    }
    fournos_status.patch_fjob_relevant_deployments("rhaiis-job", "psap-automation", reference)

    command = shlex.split(captured["command"])
    assert "--subresource=status" in command
    assert "--type=merge" in command
    patch_json = command[command.index("-p") + 1]
    assert json.loads(patch_json) == relevant_deployments_status_patch(reference)
    assert "KUBECONFIG" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["capture_stderr"] is True
    assert captured["kwargs"]["timeout"] == 10


def test_patch_fjob_status_reports_command_failure(monkeypatch) -> None:
    _enable_fournos(monkeypatch)

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=1, stderr="forbidden")

    monkeypatch.setattr(fournos_status.run, "run", fake_run)

    with pytest.raises(RuntimeError, match="forbidden"):
        fournos_status.patch_fjob_relevant_deployments("rhaiis-job", "psap-automation", None)


def test_patch_fjob_status_is_skipped_outside_fournos(monkeypatch) -> None:
    monkeypatch.setattr(fournos_status.env, "running_inside_fournos", lambda: False)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("oc patch must not run outside Fournos")

    monkeypatch.setattr(fournos_status.run, "run", fail_if_called)

    fournos_status.patch_fjob_relevant_deployments("", "psap-automation", None)
