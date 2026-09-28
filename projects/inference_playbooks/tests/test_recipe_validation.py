from pathlib import Path

import pytest
import yaml

from projects.inference_playbooks.orchestration.recipe_validation import validate_recipe_catalog


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
                    {"manifest": "models/hf.yaml"},
                    {
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


def test_validate_recipe_catalog_rejects_pvc_without_source(tmp_path: Path) -> None:
    _write(tmp_path, "models/pvc.yaml", _manifest("pvc://test-model-pvc"))
    _write(
        tmp_path,
        "tests/forge/recipes.yaml",
        "schema_version: 1\nrecipes:\n  - manifest: models/pvc.yaml\n",
    )

    with pytest.raises(ValueError, match="must declare model_source"):
        validate_recipe_catalog(tmp_path)


def test_validate_recipe_catalog_rejects_paths_outside_repository(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/forge/recipes.yaml",
        "schema_version: 1\nrecipes:\n  - manifest: ../outside.yaml\n",
    )

    with pytest.raises(ValueError, match="stay inside the repository"):
        validate_recipe_catalog(tmp_path)
