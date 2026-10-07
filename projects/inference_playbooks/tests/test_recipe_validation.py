import copy
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


def _kpi_recipe_metadata() -> dict:
    return {
        "platform": {"version": "v1.0.0"},
        "deployment_mode": "tp1",
        "hardware_profile_data": {"accelerators": {"vendor": "nvidia", "model": "H200"}},
    }


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
    _write(
        tmp_path,
        "hardware-profiles/h200.yaml",
        "profile_id: h200\naccelerators:\n  vendor: nvidia\n  model: H200\n",
    )
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
    leaderTemplate:
      spec:
        containers:
          - name: vllm
            volumeMounts: [{name: model, mountPath: /models}]
        volumes: [{name: model, emptyDir: {}}]
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
        """apiVersion: v1
kind: Service
metadata: {name: kimi}
spec:
  ports: [{port: 8000}]
  selector: {app: kimi}
""",
    )

    recipe = load_recipe_catalog(tmp_path)["kimi-k3-vllm-h200-pp2-tp8"]
    assert recipe["recipe_type"] == "recipe-v3"
    assert recipe["manifest_data"]["kind"] == "LeaderWorkerSet"
    assert recipe["auxiliary_manifests"][0]["data"]["kind"] == "Service"
    assert recipe["model_name"] == "moonshotai/Kimi-K3"
    assert recipe["model_cache"]["volume_name"] == "model"
    assert recipe["hardware_profile_data"]["accelerators"]["model"] == "H200"


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
        _config(
            {
                "inference_playbooks.namespace": "target-namespace",
                "inference_playbooks.workload": "profile1",
            }
        ),
    )
    recipe = {
        "recipe_type": "llmisvc",
        "model_name": "test/model",
        **_kpi_recipe_metadata(),
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
        "_run_workload",
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


def test_lws_launch_uses_leader_service_and_target_rdma_override(
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
                "inference_playbooks.workload": "profile1",
                "inference_playbooks.rdma_resource": "example.com/roce",
                "inference_playbooks.lws_ready_timeout_seconds": 14400,
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
    pod_template = {
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
    }
    lws = {
        "apiVersion": "leaderworkerset.x-k8s.io/v1",
        "kind": "LeaderWorkerSet",
        "metadata": {"name": "kimi"},
        "spec": {
            "leaderWorkerTemplate": {
                "leaderTemplate": copy.deepcopy(pod_template),
                "workerTemplate": copy.deepcopy(pod_template),
            }
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "kimi-api"},
        "spec": {"ports": [{"port": 8000}], "selector": {"app": "kimi"}},
    }
    recipe = {
        "recipe_type": "recipe-v3",
        "model_id": "example-model",
        "model_name": "example/model",
        **_kpi_recipe_metadata(),
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
    deployment_calls = []
    actions = []
    cache_runs = []
    captures = []
    deleted = []
    monkeypatch.setattr(test_phase.vault, "get_vault_content_path", lambda *_args: None)
    monkeypatch.setattr(
        test_phase.prepare_hf_model_cache,
        "run",
        lambda **kwargs: cache_runs.append(kwargs),
    )

    def oc(*args, **kwargs):
        if args[0] == "delete":
            deleted.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_phase, "oc", oc)

    def deploy_lws(**kwargs):
        deployment_calls.append(kwargs)
        service_manifest = yaml.safe_load(Path(kwargs["service_manifest_path"]).read_text())
        lws_manifest = yaml.safe_load(Path(kwargs["leader_worker_set_manifest_path"]).read_text())
        applied.extend((service_manifest, lws_manifest))
        return (
            f"http://{service_manifest['metadata']['name']}.{kwargs['namespace']}"
            f".svc.cluster.local:{service_manifest['spec']['ports'][0]['port']}"
        )

    monkeypatch.setattr(test_phase.deploy_lws, "run", deploy_lws)
    monkeypatch.setattr(
        test_phase.capture_lws_state,
        "run",
        lambda **kwargs: captures.append(kwargs),
    )
    monkeypatch.setattr(
        test_phase,
        "_run_workload",
        lambda *args: actions.append(args),
    )

    test_phase._launch_recipe("kimi-k3-vllm-h200-pp2-tp8", recipe)

    deployed_service, deployed_lws = applied
    lws_name = deployed_lws["metadata"]["name"]
    service_name = deployed_service["metadata"]["name"]
    run_label = deployed_lws["metadata"]["labels"]["forge.openshift.io/run"]
    assert lws_name != service_name
    assert run_label == lws_name
    assert deployed_service["spec"]["selector"] == {
        "forge.openshift.io/run": run_label,
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
        assert deployed_resources["requests"]["example.com/roce"] == "1"
        assert "rdma/ib" not in deployed_resources["requests"]
        volume = next(
            volume
            for volume in templates[template]["spec"]["volumes"]
            if volume["name"] == "weights"
        )
        assert volume["persistentVolumeClaim"]["claimName"].startswith("test-cache-example-model-")
        assert "hostPath" not in volume
    assert actions[0][0].endswith(":8000")
    assert actions[0][2] == lws_name
    assert captures == [
        {
            "namespace": "target-namespace",
            "lws_name": lws_name,
            "service_name": service_name,
            "pod_selector": f"forge.openshift.io/run={run_label}",
        }
    ]
    assert len(deployment_calls) == 1
    assert deployment_calls[0]["timeout_seconds"] == 14400
    assert ("delete", "leaderworkerset", lws_name) == deleted[0][:3]
    assert ("delete", "service", service_name) == deleted[1][:3]


def test_cleanup_failure_does_not_mask_deployment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        test_phase.config,
        "project",
        _config({"inference_playbooks.workload": "profile1"}),
    )

    def fail_deploy():
        raise RuntimeError("deployment failed")

    def fail_cleanup():
        raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="deployment failed"):
        test_phase._deploy_benchmark_finalize(
            "recipe",
            {"model_name": "model", **_kpi_recipe_metadata()},
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
    metadata = []
    kpi_labels = test_phase._benchmark_kpi_labels(
        "test-recipe",
        {"model_name": "moonshotai/Kimi-K3", **_kpi_recipe_metadata()},
        "profile1",
    )
    assert kpi_labels == {
        "model_name": "moonshotai/Kimi-K3",
        "product_version": "v1.0.0",
        "deployment_profile": "tp1",
        "guidellm_loadshape": "profile1",
        "gpu_type": "NVIDIA-H200",
        "platform": "OCP",
        "test_harness": "forge-inference-playbooks",
        "benchmark_key": "profile1",
    }
    monkeypatch.setattr(
        test_phase,
        "create_test_metadata",
        lambda _directory, labels, **kwargs: metadata.append((labels, kwargs["kpi_labels"])),
    )
    monkeypatch.setattr(
        test_phase.run_guidellm_benchmark,
        "run",
        lambda **kwargs: calls.append(kwargs),
    )

    workload_key, workload = test_phase._get_workload_config()
    test_phase._run_workload(
        "http://model:8000",
        "namespace",
        "run",
        "moonshotai/Kimi-K3",
        workload_key,
        workload,
        kpi_labels,
    )

    assert len(calls) == 2
    assert "--max-seconds=75" in calls[0]["guidellm_args"]
    assert "--rate=1,50,100,200,300" in calls[1]["guidellm_args"]
    assert "--max-seconds=275" in calls[1]["guidellm_args"]
    assert "--rampup=35" in calls[1]["guidellm_args"]
    assert "--data=prompt_tokens=1000,output_tokens=1000" in calls[1]["guidellm_args"]
    assert metadata == [
        ({"phase": "warmup", "skip": True}, kpi_labels),
        ({"phase": "benchmark"}, kpi_labels),
    ]


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
