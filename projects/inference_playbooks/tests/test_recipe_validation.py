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


def test_recipe_launch_deploys_a_unique_service_and_cleans_it_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "kubeconfig"))
    monkeypatch.setenv("CLUSTERLESS_MODE", "false")
    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        test_phase.config,
        "project",
        SimpleNamespace(get_config=lambda key: "target-namespace"),
    )

    manifest = {
        "apiVersion": "serving.kserve.io/v1alpha2",
        "kind": "LLMInferenceService",
        "metadata": {"name": "qwen-pp-validation", "namespace": "source-namespace"},
        "spec": {"model": {"uri": "hf://Qwen/Qwen2.5-7B-Instruct", "name": "qwen"}},
    }
    deleted = []

    def deploy(**kwargs: object) -> str:
        manifest_path = Path(str(kwargs["inference_service_manifest_path"]))
        deployed = yaml.safe_load(manifest_path.read_text())
        assert deployed["metadata"]["name"].startswith("qwen-pp-validation-")
        assert deployed["metadata"]["namespace"] == "target-namespace"
        assert kwargs["gateway_status_address_name"] is None
        assert kwargs["deploy_monitor"] is False
        return "http://qwen.example.test"

    def oc(*args: str, **kwargs: object) -> SimpleNamespace:
        deleted.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_phase.deploy_llmisvc, "run", deploy)
    monkeypatch.setattr(test_phase, "oc", oc)

    test_phase._launch_recipe("qwen-pp-validation", {"manifest_data": manifest})

    assert len(deleted) == 1
    assert deleted[0][:2] == ("delete", "llminferenceservice")
    assert deleted[0][2].startswith("qwen-pp-validation-")
    assert deleted[0][3:] == (
        "-n",
        "target-namespace",
        "--ignore-not-found=true",
        "--timeout=120s",
    )


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
