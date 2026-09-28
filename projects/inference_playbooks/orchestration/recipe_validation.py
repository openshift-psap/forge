"""Clusterless checks for the Inference Playbooks recipe inputs."""

from pathlib import Path
from typing import Any

import yaml

CATALOG = "tests/forge/recipes.yaml"


def validate_recipe_catalog(repository_path: Path) -> int:
    """Validate the selected LLMInferenceService manifests and their model sources."""
    root = repository_path.resolve(strict=True)
    catalog_path = _repository_file(root, CATALOG, "recipe catalog")
    catalog = _load_yaml(catalog_path)

    if (
        not isinstance(catalog, dict)
        or type(catalog.get("schema_version")) is not int
        or catalog["schema_version"] != 1
    ):
        raise ValueError(f"{CATALOG} must define schema_version: 1")
    recipes = catalog.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        raise ValueError(f"{CATALOG} must define a non-empty recipes list")

    for index, recipe in enumerate(recipes, start=1):
        label = f"{CATALOG} recipe {index}"
        if not isinstance(recipe, dict):
            raise ValueError(f"{label} must be a mapping")
        manifest_ref = recipe.get("manifest")
        manifest_path = _repository_file(root, manifest_ref, f"{label} manifest")
        manifest = _load_yaml(manifest_path)
        _validate_manifest(manifest, manifest_path)
        _validate_model_source(root, recipe.get("model_source"), manifest, label)

    return len(recipes)


def _repository_file(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a repository-relative file path")
    relative_path = Path(value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"{label} must stay inside the repository")

    try:
        path = (root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} does not exist: {value}") from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"{label} must be a file inside the repository: {value}")
    return path


def _load_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            documents = list(yaml.safe_load_all(stream))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}") from exc
    documents = [document for document in documents if document is not None]
    if len(documents) != 1:
        raise ValueError(f"Expected one YAML document in {path}, found {len(documents)}")
    return documents[0]


def _validate_manifest(manifest: Any, path: Path) -> None:
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} must contain an LLMInferenceService mapping")
    if manifest.get("apiVersion") != "serving.kserve.io/v1alpha2":
        raise ValueError(f"{path} must use serving.kserve.io/v1alpha2")
    if manifest.get("kind") != "LLMInferenceService":
        raise ValueError(f"{path} must be an LLMInferenceService")

    metadata = manifest.get("metadata")
    model = (
        manifest.get("spec", {}).get("model") if isinstance(manifest.get("spec"), dict) else None
    )
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
    ):
        raise ValueError(f"{path} must define metadata.name")
    if not isinstance(model, dict):
        raise ValueError(f"{path} must define spec.model")
    for field in ("uri", "name"):
        if not isinstance(model.get(field), str) or not model[field]:
            raise ValueError(f"{path} must define spec.model.{field}")


def _validate_model_source(root: Path, source: Any, manifest: dict[str, Any], label: str) -> None:
    model_uri = manifest["spec"]["model"]["uri"]
    if model_uri.startswith("hf://") and model_uri.removeprefix("hf://"):
        return
    if not model_uri.startswith("pvc://"):
        raise ValueError(f"{label} uses an unsupported model URI: {model_uri}")

    if not isinstance(source, dict):
        raise ValueError(f"{label} uses a PVC URI and must declare model_source")
    source_uri = source.get("source_uri")
    if (
        not isinstance(source_uri, str)
        or not source_uri.startswith("hf://")
        or not source_uri.removeprefix("hf://")
    ):
        raise ValueError(f"{label} model_source.source_uri must be an HF URI")
    if source.get("storage") != "prepopulated-pvc":
        raise ValueError(f"{label} must declare model_source.storage: prepopulated-pvc")

    guide_path = _repository_file(
        root, source.get("preparation_guide"), f"{label} preparation guide"
    )
    guide = guide_path.read_text(encoding="utf-8")
    pvc_name = model_uri.removeprefix("pvc://").split("/", maxsplit=1)[0]
    if not pvc_name or source_uri not in guide or pvc_name not in guide:
        raise ValueError(f"{label} preparation guide must document {source_uri} and PVC {pvc_name}")
    if not _contains_pvc_claim(manifest, pvc_name):
        raise ValueError(f"{label} manifest must mount PVC {pvc_name}")
    storage_initializer = manifest["spec"].get("storageInitializer")
    if not isinstance(storage_initializer, dict) or storage_initializer.get("enabled") is not False:
        raise ValueError(f"{label} PVC-backed manifest must disable the storage initializer")


def _contains_pvc_claim(value: Any, pvc_name: str) -> bool:
    if isinstance(value, dict):
        claim = value.get("persistentVolumeClaim")
        if isinstance(claim, dict) and claim.get("claimName") == pvc_name:
            return True
        return any(_contains_pvc_claim(child, pvc_name) for child in value.values())
    if isinstance(value, list):
        return any(_contains_pvc_claim(child, pvc_name) for child in value)
    return False
