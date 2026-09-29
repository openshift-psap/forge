from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from projects.inference_playbooks.orchestration import test_phase
from projects.inference_playbooks.orchestration.recipe_validation import (
    load_recipe_catalog,
    validate_recipe_catalog,
)


def _write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _manifest(uri: str) -> str:
    return f"""apiVersion: serving.kserve.io/v1alpha2
kind: LLMInferenceService
metadata:
  name: test-model
spec:
  model:
    uri: {uri}
    name: test/model
  storageInitializer:
    enabled: false
  template:
    volumes:
      - name: model
        persistentVolumeClaim:
          claimName: test-model-pvc
"""


def _config(values: dict):
    return SimpleNamespace(get_config=lambda key: values.get(key))


def test_validate_recipe_catalog_accepts_hf_and_documented_pvc(tmp_path: Path) -> None:
    _write(tmp_path, "models/hf.yaml", _manifest("hf://test/model"))
    _write(tmp_path, "models/pvc.yaml", _manifest("pvc://test-model-pvc"))
    _write(tmp_path, "guides/model.md", "Source: hf://test/model\nPVC: test-model-pvc\n")
    _write(
        tmp_path,
        "tests/forge/recipes.yaml",
        yaml.safe_dump(
            {
                "schema_version": 1,
                "recipes": [
                    {"id": "hf-model", "manifest": "models/hf.yaml"},
                    {
                        "id": "pvc-model",
                        "manifest": "models/pvc.yaml",
                        "model_source": {
                            "source_uri": "hf://test/model",
                            "storage": "prepopulated-pvc",
                            "preparation_guide": "guides/model.md",
                        },
                    },
                ],
            }
        ),
    )

    assert validate_recipe_catalog(tmp_path) == 2
    assert set(load_recipe_catalog(tmp_path)) == {"hf-model", "pvc-model"}


def test_validate_recipe_catalog_rejects_substring_provenance(tmp_path: Path) -> None:
    _write(tmp_path, "models/pvc.yaml", _manifest("pvc://test-model-pvc"))
    _write(
        tmp_path,
        "guides/model.md",
        "Source: hf://test/model-old\nPVC: test-model-pvc-old\n",
    )
    _write(
        tmp_path,
        "tests/forge/recipes.yaml",
        yaml.safe_dump(
            {
                "schema_version": 1,
                "recipes": [
                    {
                        "id": "pvc-model",
                        "manifest": "models/pvc.yaml",
                        "model_source": {
                            "source_uri": "hf://test/model",
                            "storage": "prepopulated-pvc",
                            "preparation_guide": "guides/model.md",
                        },
                    }
                ],
            }
        ),
    )

    with pytest.raises(ValueError, match="must document"):
        validate_recipe_catalog(tmp_path)


def test_load_recipe_v3_discovers_lws_and_service(tmp_path: Path) -> None:
    recipe_dir = "models/kimi-k3/vllm/kimi-k3/recipes/h200/profile/pp2-tp8"
    _write(
        tmp_path,
        "models/kimi-k3/model.yaml",
        "schema_version: 1\nmodel_id: kimi-k3\nhuggingface_id: moonshotai/Kimi-K3\n",
    )
    _write(tmp_path, "hardware-profiles/h200.yaml", "profile_id: h200\n")
    _write(
        tmp_path,
        f"{recipe_dir}/recipe.yaml",
        """schema_version: 3
recipe_id: kimi-k3-vllm-h200-pp2-tp8
model_id: kimi-k3
hardware_profile: hardware-profiles/h200.yaml
deployment:
  model_cache: {volume_name: model, pvc_size: 30Gi, model_directory_name: model}
  components:
    modelserver: {kind: LeaderWorkerSet, source: config/lws.yaml}
  auxiliary_sources:
    - {path: config/service.yaml, kind: Service}
""",
    )
    _write(
        tmp_path,
        f"{recipe_dir}/config/lws.yaml",
        """apiVersion: leaderworkerset.x-k8s.io/v1
kind: LeaderWorkerSet
metadata: {name: kimi}
spec:
  leaderWorkerTemplate:
    workerTemplate:
      spec:
        containers:
          - name: vllm
            volumeMounts: [{name: model, mountPath: /models}]
        volumes: [{name: model, emptyDir: {}}]
""",
    )
    _write(
        tmp_path,
        f"{recipe_dir}/config/service.yaml",
        "apiVersion: v1\nkind: Service\nmetadata: {name: kimi}\nspec: {}\n",
    )

    recipe = load_recipe_catalog(tmp_path)["kimi-k3-vllm-h200-pp2-tp8"]
    assert recipe["recipe_type"] == "recipe-v3"
    assert recipe["manifest_data"]["kind"] == "LeaderWorkerSet"
    assert recipe["auxiliary_manifests"][0]["data"]["kind"] == "Service"
    assert recipe["model_name"] == "moonshotai/Kimi-K3"
    assert recipe["model_cache"]["volume_name"] == "model"


def test_validate_recipe_catalog_rejects_duplicate_ids(tmp_path: Path) -> None:
    _write(tmp_path, "models/hf.yaml", _manifest("hf://test/model"))
    _write(
        tmp_path,
        "tests/forge/recipes.yaml",
        "schema_version: 1\nrecipes:\n  - id: duplicate\n    manifest: models/hf.yaml\n  - id: duplicate\n    manifest: models/hf.yaml\n",
    )

    with pytest.raises(ValueError, match="id must be unique"):
        validate_recipe_catalog(tmp_path)


def test_recipe_launch_requires_target_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("CLUSTERLESS_MODE", "true")

    with pytest.raises(RuntimeError, match="needs a target cluster"):
        test_phase._launch_recipe("qwen-pp-validation", {})


def test_llmisvc_launch_benchmarks_captures_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "kubeconfig"))
    monkeypatch.setenv("CLUSTERLESS_MODE", "false")
    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        test_phase.config,
        "project",
        _config({"inference_playbooks.namespace": "target-namespace"}),
    )
    recipe = {
        "recipe_type": "llmisvc",
        "model_name": "test/model",
        "model_source": {"source_uri": "hf://test/model"},
        "manifest_data": yaml.safe_load(_manifest("pvc://test-model-pvc")),
    }
    actions = []

    def deploy(**kwargs: object) -> str:
        deployed = yaml.safe_load(Path(str(kwargs["inference_service_manifest_path"])).read_text())
        assert deployed["metadata"]["name"].startswith("test-model-")
        assert deployed["metadata"]["namespace"] == "target-namespace"
        return "http://qwen.example.test"

    def oc(*args: str, **kwargs: object) -> SimpleNamespace:
        actions.append(args)
        if args[:2] == ("get", "pvc"):
            return SimpleNamespace(returncode=0, stdout='{"status":{"phase":"Bound"}}', stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_phase.deploy_llmisvc, "run", deploy)
    monkeypatch.setattr(
        test_phase.capture_llmisvc_state,
        "run",
        lambda **kwargs: actions.append(("capture", kwargs["llmisvc_name"])),
    )
    monkeypatch.setattr(
        test_phase,
        "_run_profile1",
        lambda *args: actions.append(("benchmark", args[0])),
    )
    monkeypatch.setattr(test_phase, "oc", oc)

    test_phase._launch_recipe("pvc-model", recipe)

    assert any(action[0] == "capture" for action in actions)
    assert ("benchmark", "http://qwen.example.test") in actions
    assert any(action[:2] == ("delete", "llminferenceservice") for action in actions)


def test_hf_llmisvc_reuses_kserve_model_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = {
        "marker_filename": ".forge-model-cache.json",
        "pvc": {
            "size": "30Gi",
            "access_mode": "ReadWriteMany",
            "storage_class_name": None,
            "name_prefix": "test-cache",
            "model_directory_name": "model",
        },
        "download": {
            "wait_timeout_seconds": 7200,
            "poll_interval_seconds": 15,
            "pod_image_pull_policy": "IfNotPresent",
        },
        "hf": {"downloader_image": "example/downloader:test"},
    }
    monkeypatch.setattr(test_phase.config, "project", _config({"model_cache": cache}))
    monkeypatch.setattr(test_phase.vault, "get_vault_content_path", lambda *_args: tmp_path / "hf")
    calls = []
    monkeypatch.setattr(
        test_phase.prepare_hf_model_cache, "run", lambda **kwargs: calls.append(kwargs)
    )
    manifest = yaml.safe_load(_manifest("hf://test/model"))

    test_phase._prepare_llmisvc_model("hf-model", {}, manifest, "target-namespace")

    assert len(calls) == 1
    assert manifest["spec"]["model"]["uri"].startswith("pvc://test-cache-hf-model-")
    assert manifest["spec"]["storageInitializer"] == {"enabled": False}


def test_lws_launch_uses_leader_service_and_janus_roce_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "kubeconfig"))
    monkeypatch.setenv("CLUSTERLESS_MODE", "false")
    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        test_phase.config,
        "project",
        _config(
            {
                "inference_playbooks.namespace": "target-namespace",
                "inference_playbooks.rdma_resource": "nvidia.com/roce",
                "model_cache": {
                    "marker_filename": "cached.marker",
                    "pvc": {
                        "size": "30Gi",
                        "access_mode": "ReadWriteMany",
                        "storage_class_name": None,
                        "name_prefix": "test-cache",
                        "model_directory_name": "model",
                    },
                    "download": {
                        "wait_timeout_seconds": 7200,
                        "poll_interval_seconds": 15,
                        "pod_image_pull_policy": "IfNotPresent",
                    },
                    "hf": {"downloader_image": "example/downloader:test"},
                },
            }
        ),
    )
    resources = {
        "requests": {"nvidia.com/gpu": "8", "rdma/ib": "1"},
        "limits": {"nvidia.com/gpu": "8", "rdma/ib": "1"},
    }
    lws = {
        "apiVersion": "leaderworkerset.x-k8s.io/v1",
        "kind": "LeaderWorkerSet",
        "metadata": {"name": "kimi"},
        "spec": {
            "leaderWorkerTemplate": {
                "workerTemplate": {
                    "metadata": {"labels": {}},
                    "spec": {
                        "containers": [
                            {
                                "name": "vllm",
                                "resources": copy_dict(resources),
                                "volumeMounts": [{"name": "weights", "mountPath": "/models"}],
                            }
                        ],
                        "volumes": [
                            {"name": "weights", "hostPath": {"path": "/models"}},
                            {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                        ],
                    },
                },
            }
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "kimi"},
        "spec": {"ports": [{"port": 8000}], "selector": {"app": "kimi"}},
    }
    recipe = {
        "recipe_type": "recipe-v3",
        "model_id": "example-model",
        "model_name": "example/model",
        "model_cache": {
            "volume_name": "weights",
            "pvc_size": "80Gi",
            "model_directory_name": "checkpoint",
            "wait_timeout_seconds": 1800,
        },
        "manifest_data": lws,
        "auxiliary_manifests": [{"data": service}],
    }
    applied = []
    actions = []
    cache_runs = []
    monkeypatch.setattr(
        test_phase.vault, "get_vault_content_path", lambda *_args: None
    )
    monkeypatch.setattr(
        test_phase.prepare_hf_model_cache,
        "run",
        lambda **kwargs: cache_runs.append(kwargs),
    )
    monkeypatch.setattr(test_phase, "oc_apply", lambda path, manifest: applied.append(manifest))
    monkeypatch.setattr(
        test_phase,
        "oc",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(test_phase, "_capture_lws_state", lambda *args: None)
    monkeypatch.setattr(
        test_phase,
        "_run_profile1",
        lambda *args: actions.append(args),
    )

    test_phase._launch_recipe("kimi-k3-vllm-h200-pp2-tp8", recipe)

    deployed_service, deployed_lws = applied
    assert deployed_service["spec"]["selector"] == {
        "forge.openshift.io/run": deployed_service["metadata"]["name"],
        "forge.openshift.io/endpoint": "leader",
    }
    templates = deployed_lws["spec"]["leaderWorkerTemplate"]
    assert "forge.openshift.io/endpoint" in templates["leaderTemplate"]["metadata"]["labels"]
    assert "forge.openshift.io/endpoint" not in templates["workerTemplate"]["metadata"]["labels"]
    assert len(cache_runs) == 1
    assert cache_runs[0]["model_key"] == "example-model"
    assert cache_runs[0]["model_uri"] == "hf://example/model"
    assert cache_runs[0]["pvc_size"] == "80Gi"
    assert cache_runs[0]["model_directory_name"] == "checkpoint"
    assert cache_runs[0]["wait_timeout_seconds"] == 1800
    for template in ("leaderTemplate", "workerTemplate"):
        deployed_resources = templates[template]["spec"]["containers"][0]["resources"]
        assert deployed_resources["requests"]["nvidia.com/roce"] == "1"
        assert "rdma/ib" not in deployed_resources["requests"]
        volume = next(
            volume
            for volume in templates[template]["spec"]["volumes"]
            if volume["name"] == "weights"
        )
        assert volume["persistentVolumeClaim"]["claimName"].startswith(
            "test-cache-example-model-"
        )
        assert "hostPath" not in volume
    assert actions[0][0].endswith(":8000")


def test_cleanup_failure_does_not_mask_deployment_failure() -> None:
    def fail_deploy():
        raise RuntimeError("deployment failed")

    def fail_cleanup():
        raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="deployment failed"):
        test_phase._deploy_benchmark_finalize(
            "recipe",
            {"model_name": "model"},
            "namespace",
            "run",
            fail_deploy,
            lambda: None,
            fail_cleanup,
        )


def test_profile1_uses_forge_workload_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        test_phase.config,
        "project",
        _config({"inference_playbooks.workload": "profile1"}),
    )
    monkeypatch.setattr(test_phase.env, "NextArtifactDir", lambda _name: nullcontext())
    calls = []
    monkeypatch.setattr(
        test_phase.run_guidellm_benchmark,
        "run",
        lambda **kwargs: calls.append(kwargs),
    )

    test_phase._run_profile1("http://model:8000", "namespace", "run", "moonshotai/Kimi-K3")

    assert len(calls) == 2
    assert "--max-seconds=75" in calls[0]["guidellm_args"]
    assert "--rate=1,50,100,200,300" in calls[1]["guidellm_args"]
    assert "--max-seconds=275" in calls[1]["guidellm_args"]
    assert "--rampup=35" in calls[1]["guidellm_args"]
    assert "--data=prompt_tokens=1000,output_tokens=1000" in calls[1]["guidellm_args"]


def test_validate_recipe_catalog_rejects_pvc_without_source(tmp_path: Path) -> None:
    _write(tmp_path, "models/pvc.yaml", _manifest("pvc://test-model-pvc"))
    _write(
        tmp_path,
        "tests/forge/recipes.yaml",
        "schema_version: 1\nrecipes:\n  - id: pvc-model\n    manifest: models/pvc.yaml\n",
    )

    with pytest.raises(ValueError, match="must declare model_source"):
        validate_recipe_catalog(tmp_path)


def test_validate_recipe_catalog_rejects_paths_outside_repository(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/forge/recipes.yaml",
        "schema_version: 1\nrecipes:\n  - id: outside\n    manifest: ../outside.yaml\n",
    )

    with pytest.raises(ValueError, match="stay inside the repository"):
        validate_recipe_catalog(tmp_path)


def copy_dict(value: dict) -> dict:
    return {key: dict(item) if isinstance(item, dict) else item for key, item in value.items()}
