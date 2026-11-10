"""RHAIIS cleanup must hold the cluster slot until target resources are gone."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from projects.guidellm.toolbox.run_guidellm_benchmark import main as guidellm
from projects.rhaiis.orchestration import prepare_rhaiis, verify_cleanup

FJOB = "forge-rhaiis-233319"


def test_job_resources_selects_only_explicit_fjob_label(monkeypatch) -> None:
    def fake_oc(*args, **kwargs):
        assert "-l" in args
        assert f"{prepare_rhaiis.FOURNOS_JOB_LABEL}={FJOB}" in args
        return SimpleNamespace(stdout="pod/labelled-pod\n")

    monkeypatch.setattr(prepare_rhaiis, "oc", fake_oc)

    assert prepare_rhaiis._job_resources("pod", "test-ns", FJOB) == ["pod/labelled-pod"]


def test_job_cleanup_propagates_list_errors(monkeypatch) -> None:
    monkeypatch.setenv("FJOB_NAME", FJOB)
    monkeypatch.setattr(prepare_rhaiis.runtime_config, "get_namespace", lambda: "test-ns")

    def fail_list(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["oc", *args])

    monkeypatch.setattr(prepare_rhaiis, "oc", fail_list)

    with pytest.raises(subprocess.CalledProcessError):
        prepare_rhaiis.cleanup()


def test_job_cleanup_propagates_delete_errors(monkeypatch) -> None:
    monkeypatch.setenv("FJOB_NAME", FJOB)
    monkeypatch.setattr(prepare_rhaiis.runtime_config, "get_namespace", lambda: "test-ns")

    def fail_delete(*args, **kwargs):
        if args[0] == "delete":
            raise subprocess.CalledProcessError(1, ["oc", *args])
        return SimpleNamespace(stdout="inferenceservice.serving.kserve.io/model-233319\n")

    monkeypatch.setattr(prepare_rhaiis, "oc", fail_delete)

    with pytest.raises(subprocess.CalledProcessError):
        prepare_rhaiis.cleanup()


def test_job_cleanup_refuses_success_when_resource_remains(monkeypatch) -> None:
    monkeypatch.setenv("FJOB_NAME", FJOB)
    monkeypatch.setattr(prepare_rhaiis.runtime_config, "get_namespace", lambda: "test-ns")
    deletes = []

    def fake_oc(*args, **kwargs):
        if args[0] == "delete":
            deletes.append(args)
            return SimpleNamespace(stdout="")
        if args[1] == "inferenceservice":
            return SimpleNamespace(stdout="inferenceservice.serving.kserve.io/model-233319\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(prepare_rhaiis, "oc", fake_oc)

    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        prepare_rhaiis.cleanup()
    assert deletes
    assert "--cascade=foreground" in deletes[0]
    assert "--wait=true" in deletes[0]


def test_job_cleanup_completes_after_labelled_resource_disappears(monkeypatch) -> None:
    monkeypatch.setenv("FJOB_NAME", FJOB)
    monkeypatch.setattr(prepare_rhaiis.runtime_config, "get_namespace", lambda: "test-ns")
    present = {"inferenceservice.serving.kserve.io/model-233319"}

    def fake_oc(*args, **kwargs):
        if args[0] == "delete":
            present.discard(args[1])
            return SimpleNamespace(stdout="")
        names = present if args[1] == "inferenceservice" else set()
        return SimpleNamespace(stdout="\n".join(names))

    monkeypatch.setattr(prepare_rhaiis, "oc", fake_oc)

    prepare_rhaiis.cleanup()

    assert not present


def test_guidellm_fjob_label_reaches_job_pods() -> None:
    manifest = {
        "kind": "Job",
        "metadata": {"labels": {"job-name": "benchmark"}},
        "spec": {"template": {"metadata": {"labels": {"job-name": "benchmark"}}}},
    }

    guidellm._with_fjob_label(manifest, FJOB)

    assert manifest["metadata"]["labels"][guidellm.FOURNOS_JOB_LABEL] == FJOB
    assert manifest["spec"]["template"]["metadata"]["labels"][guidellm.FOURNOS_JOB_LABEL] == FJOB
    assert manifest["metadata"]["labels"]["job-name"] == "benchmark"


def test_guidellm_job_creation_applies_fjob_label(monkeypatch, tmp_path) -> None:
    args = SimpleNamespace(
        artifact_dir=tmp_path,
        config_path=None,
        use_pvc=False,
        image="guidellm-image",
        endpoint_url="http://model",
        timeout=10,
        hf_token_secret="",
        fs_group=None,
        replica_count=1,
        validate_backend=True,
        fjob_name=FJOB,
    )
    ctx = SimpleNamespace(
        target_namespace="test-ns",
        benchmark_name="benchmark",
        guidellm_args=[],
        image="guidellm-image",
    )
    applied = []
    monkeypatch.setattr(
        guidellm,
        "render_guidellm_shared_volume_job_from_parts",
        lambda **kwargs: {"kind": "Job", "metadata": {}, "spec": {"template": {"metadata": {}}}},
    )
    monkeypatch.setattr(guidellm, "oc_apply", lambda path, manifest: applied.append(manifest))

    guidellm.create_guidellm_resources_task.original_func(args, ctx)

    labels = applied[0]["spec"]["template"]["metadata"]["labels"]
    assert labels[guidellm.FOURNOS_JOB_LABEL] == FJOB


def test_guidellm_auth_secret_is_labelled_without_logging_its_data(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "dummy-token"
    token_file.write_text("dummy")
    args = SimpleNamespace(api_key_file_path=str(token_file), fjob_name=FJOB)
    ctx = SimpleNamespace(benchmark_name="benchmark", target_namespace="test-ns")
    calls = []

    def fake_oc(*oc_args, **kwargs):
        calls.append((oc_args, kwargs))
        if oc_args[0] == "create":
            return SimpleNamespace(
                stdout=json.dumps(
                    {"kind": "Secret", "metadata": {"name": "benchmark-auth-token"}, "data": {}}
                )
            )
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(guidellm, "oc", fake_oc)

    guidellm.create_api_key_secret.original_func(args, ctx)

    apply_args, apply_kwargs = next(call for call in calls if call[0][0] == "apply")
    assert apply_args == ("apply", "-f", "-")
    assert apply_kwargs["handled_secretly"] is True
    manifest = json.loads(apply_kwargs["input_text"])
    assert manifest["metadata"]["labels"][guidellm.FOURNOS_JOB_LABEL] == FJOB


def _write_manifest(tmp_path, filename: str, kind: str, name: str) -> None:
    src = tmp_path / "01__test" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / filename).write_text(
        yaml.safe_dump({"kind": kind, "metadata": {"name": name, "namespace": "test-ns"}})
    )


def test_stable_verifier_catches_old_pr_job_with_truncated_suffix(monkeypatch, tmp_path) -> None:
    job_name = "guidellm-bench-long-workload-long-model-name"
    _write_manifest(tmp_path, "guidellm-job.yaml", "Job", job_name)

    def fake_oc_names(*args):
        if args[:3] == ("get", "pod", "-n"):
            return [f"pod/{job_name}-abcde"]
        return []

    monkeypatch.setattr(verify_cleanup, "_oc_names", fake_oc_names)

    with pytest.raises(RuntimeError, match="pod"):
        verify_cleanup.verify_cleanup(tmp_path, FJOB)


def test_stable_verifier_passes_when_manifest_resources_are_gone(monkeypatch, tmp_path) -> None:
    _write_manifest(tmp_path, "inferenceservice.yaml", "InferenceService", "model-233319")
    monkeypatch.setattr(verify_cleanup, "_oc_names", lambda *args: [])

    verify_cleanup.verify_cleanup(tmp_path, FJOB)


def test_stable_verifier_ignores_unrelated_resource_with_same_time_suffix(
    monkeypatch, tmp_path
) -> None:
    _write_manifest(tmp_path, "inferenceservice.yaml", "InferenceService", "model-233319")

    def fake_oc_names(*args):
        if args[:3] == ("get", "pod", "-n"):
            return ["pod/unrelated-233319-abcde"]
        return []

    monkeypatch.setattr(verify_cleanup, "_oc_names", fake_oc_names)

    verify_cleanup.verify_cleanup(tmp_path, FJOB)


def test_stable_verifier_holds_quota_when_test_has_no_ownership_evidence(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "03__test").mkdir()
    monkeypatch.setattr(verify_cleanup, "_oc_names", lambda *args: [])

    with pytest.raises(RuntimeError, match="no known resource manifest"):
        verify_cleanup.verify_cleanup(tmp_path, FJOB)


def test_stable_verifier_uses_hub_test_status_if_artifacts_were_removed(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(verify_cleanup, "_oc_names", lambda *args: [])

    with pytest.raises(RuntimeError, match="no known resource manifest"):
        verify_cleanup.verify_cleanup(tmp_path, FJOB, test_started=True)


def test_stable_verifier_fails_closed_on_query_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="sensitive token")

    monkeypatch.setattr(verify_cleanup.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc:
        verify_cleanup._oc_names("get", "secret", "-A", "-o", "name")
    assert "sensitive token" not in str(exc.value)
