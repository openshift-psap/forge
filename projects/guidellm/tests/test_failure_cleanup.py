"""Exercise the real GuideLLM DSL failure/finalizer ordering without a cluster."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from projects.core.dsl import script_manager
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.library import env
from projects.guidellm.toolbox.run_guidellm_benchmark import main as benchmark


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    # Core DSL tests reset the global registry. Register this module's real task
    # metadata in an isolated manager rather than mocking execute_tasks/always.
    manager = script_manager.ScriptManager()
    monkeypatch.setattr(script_manager, "_script_manager", manager)
    monkeypatch.chdir(env.FORGE_HOME)
    for function in vars(benchmark).values():
        info = getattr(function, "_task_info", None)
        if info is not None:
            manager.register_task(info, info["id"].rsplit(":", 1)[0])
    artifact = tmp_path / "artifacts"
    monkeypatch.setenv("ARTIFACT_DIR", str(artifact))
    monkeypatch.setattr(env, "BASE_ARTIFACT_DIR", None)
    env.init()

    state = SimpleNamespace(
        resources={},
        events=[],
        deletes=[],
        notifications=[],
        failure=None,
        create_failure=None,
        malformed_response=False,
        delete_failure=None,
        replace_job=False,
        sidecar_running=False,
    )

    def result(stdout="", *, returncode=0, stderr=""):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def oc(*args, **kwargs):
        if args[0] == "create":
            document = yaml.safe_load(Path(args[2]).read_text())
            kind = document["kind"]
            if kind == state.create_failure:
                raise subprocess.CalledProcessError(1, "oc create", stderr="AlreadyExists")
            document["metadata"]["uid"] = f"owned-{kind}"
            state.resources[kind] = document
            state.events.append(f"create:{kind}")
            if kind == "Job":
                state.sidecar_running = True
            if state.malformed_response:
                document = {"kind": kind, "metadata": {}}
            return result(json.dumps(document))
        if args[0] == "delete":
            if args[1].startswith("--raw="):
                url = args[1].removeprefix("--raw=")
                resource = url.split("/")[-2]
                kind = {
                    "jobs": "Job",
                    "pods": "Pod",
                    "persistentvolumeclaims": "PersistentVolumeClaim",
                }[resource]
                options = json.loads(kwargs["input_text"])
                state.deletes.append((url, options, kwargs))
                assert kwargs["timeout_seconds"] <= 180
                assert options["propagationPolicy"] == "Foreground"
                current = state.resources.get(kind)
                if current is None:
                    return result(returncode=1, stderr="Error from server (NotFound)")
                if current["metadata"]["uid"] != options["preconditions"]["uid"]:
                    return result(returncode=1, stderr="Error from server (Conflict)")
                if kind == state.delete_failure:
                    return result(returncode=1, stderr="Error from server (Forbidden)")
                state.resources.pop(kind)
                state.events.append(f"delete:{kind}")
                if kind == "Job":
                    state.sidecar_running = False
            else:
                # The existing pre-run cleanup has no resources to delete here.
                assert not state.resources
            return result()
        if args[:2] == ("get", "pods"):
            return result("" if "--no-headers" in args else "benchmark-pod")
        if args[:2] == ("get", "pod"):
            if state.failure == "timeout":
                raise TimeoutError("synthetic benchmark timeout")
            return result(json.dumps({"terminated": {"exitCode": int(state.failure == "main")}}))
        if args[:2] == ("get", "job"):
            if state.failure == "timeout":
                raise TimeoutError("synthetic benchmark timeout")
            if "jsonpath={.status.active}" in args:
                return result()
            return result(
                str(int(("jsonpath={.status.failed}" in args) == (state.failure == "main")))
            )
        pytest.fail(f"Unexpected oc call: {args}")

    def get_json(kind, **kwargs):
        if kind == "pods":
            return {"items": [{"spec": {"nodeName": "synthetic-node"}}]}
        if kind == "pod":
            return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        assert kind == "job"
        return state.resources["Job"]

    def capture(**kwargs):
        state.events.append("capture")
        if state.replace_job:
            state.resources["Job"]["metadata"]["uid"] = "replacement"
        if state.failure == "capture":
            raise RuntimeError("synthetic diagnostic capture failure")

    def extract(*args):
        state.events.append("extract")
        if state.failure == "extract":
            raise RuntimeError("synthetic result extraction failure")
        return "synthetic results collected"

    monkeypatch.setattr(benchmark, "oc", oc)
    monkeypatch.setattr(benchmark, "oc_get_json", get_json)
    # Also intercept shared helpers so a regression cannot invoke a real oc.
    monkeypatch.setattr("projects.core.dsl.utils.k8s.oc", oc)
    monkeypatch.setattr(benchmark, "capture_guidellm_state", capture)
    monkeypatch.setattr(benchmark, "extract_results_from_sidecar", extract)
    monkeypatch.setattr(benchmark, "extract_results_from_copy_pod", extract)
    monkeypatch.setattr(
        "projects.core.library.ci.add_notification_file",
        lambda *args, **kwargs: state.notifications.append(args),
    )

    def run(**kwargs):
        return benchmark.run(
            endpoint_url="https://model.example.test",
            name="cleanup-test",
            namespace="test-namespace",
            **kwargs,
        )

    state.run = run
    return state


@pytest.mark.parametrize("use_pvc", [False, True])
@pytest.mark.parametrize("failure", ["main", "timeout", "extract", "capture"])
def test_failure_captures_diagnostics_then_deletes_owned_resources(cluster, use_pvc, failure):
    cluster.failure = failure
    with pytest.raises(TaskExecutionError):
        cluster.run(use_pvc=use_pvc)
    assert cluster.resources == {}
    assert not cluster.sidecar_running
    assert cluster.events.index("capture") < cluster.events.index("delete:Job")
    if failure in {"main", "timeout", "capture"}:
        assert "extract" not in cluster.events
    assert cluster.deletes
    assert all(
        options["preconditions"]["uid"].startswith("owned-") for _, options, _ in cluster.deletes
    )


@pytest.mark.parametrize("use_pvc", [False, True])
def test_success_extracts_results_before_cleanup(cluster, use_pvc):
    assert cluster.run(use_pvc=use_pvc) == 0
    assert cluster.resources == {}
    assert cluster.events.index("capture") < cluster.events.index("extract")
    assert cluster.events.index("extract") < cluster.events.index("delete:Job")


def test_early_validation_failure_does_not_guess_cleanup_targets(cluster):
    existing = {"metadata": {"name": "model-storage", "uid": "preexisting"}}
    cluster.resources["PersistentVolumeClaim"] = existing
    with pytest.raises(TaskExecutionError) as error:
        cluster.run(guidellm_args=["--max-requests={rate}"])
    assert isinstance(error.value.__cause__, ValueError)
    assert cluster.events == []
    assert cluster.deletes == []
    assert cluster.resources == {"PersistentVolumeClaim": existing}


@pytest.mark.parametrize("kind", ["Job", "PersistentVolumeClaim", "Pod"])
def test_partial_creation_failure_cleans_only_proven_resources(cluster, kind):
    cluster.create_failure = kind
    with pytest.raises(TaskExecutionError):
        cluster.run(use_pvc=True)
    assert cluster.resources == {}
    assert f"delete:{kind}" not in cluster.events
    assert all(
        options["preconditions"]["uid"] != f"owned-{kind}" for _, options, _ in cluster.deletes
    )


def test_unknown_creation_response_does_not_guess_ownership(cluster):
    cluster.malformed_response = True
    with pytest.raises(TaskExecutionError, match="ownership"):
        cluster.run()
    assert "Job" in cluster.resources
    assert cluster.deletes == []


def test_cleanup_refuses_replacement_and_preserves_benchmark_error(cluster):
    cluster.failure = "main"
    cluster.replace_job = True
    with pytest.raises(ExceptionGroup) as error:
        cluster.run()
    assert len(error.value.exceptions) == 2
    assert "failed with exit code 1" in str(error.value.exceptions[0])
    assert "clean up" in str(error.value.exceptions[1])
    assert cluster.resources["Job"]["metadata"]["uid"] == "replacement"
    assert cluster.notifications


def test_cleanup_failure_is_visible_and_other_resources_are_still_attempted(cluster):
    cluster.delete_failure = "Job"
    with pytest.raises(TaskExecutionError, match="clean"):
        cluster.run(use_pvc=True)
    assert set(cluster.resources) == {"Job"}
    assert cluster.notifications


def test_copy_pod_cleanup_failure_does_not_skip_job_cleanup(cluster):
    cluster.delete_failure = "Pod"
    with pytest.raises(TaskExecutionError, match="cleanup_copy_pod"):
        cluster.run(use_pvc=True)
    assert set(cluster.resources) == {"Pod"}
    assert "delete:Job" in cluster.events
    assert "delete:PersistentVolumeClaim" in cluster.events
    assert cluster.notifications


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("oc", 60), OSError("synthetic error")]
)
def test_cleanup_command_exception_does_not_prevent_other_deletions(cluster, monkeypatch, failure):
    original_oc = benchmark.oc

    def fail_job_delete(*args, **kwargs):
        if args[0] == "delete" and args[1].startswith("--raw=/apis/batch/v1/"):
            raise failure
        return original_oc(*args, **kwargs)

    monkeypatch.setattr(benchmark, "oc", fail_job_delete)
    with pytest.raises(TaskExecutionError, match="clean up"):
        cluster.run(use_pvc=True)
    assert set(cluster.resources) == {"Job"}
    assert cluster.notifications


def test_cleanup_is_idempotent(cluster):
    cluster.run()
    ctx = SimpleNamespace(
        created_resources={
            "job": {
                "kind": "Job",
                "metadata": {
                    "name": "cleanup-test",
                    "namespace": "test-namespace",
                    "uid": "owned-Job",
                },
            },
        }
    )
    benchmark.cleanup_guidellm_resources(SimpleNamespace(use_pvc=False), ctx)
    assert cluster.resources == {}
