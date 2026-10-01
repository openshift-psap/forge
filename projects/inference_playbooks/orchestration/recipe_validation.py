"""Clusterless checks for Inference Playbooks recipe inputs."""

import re
from pathlib import Path
from typing import Any

import yaml

CATALOG = "tests/forge/recipes.yaml"
RECIPE_GLOB = "models/*/*/*/recipes/*/*/*/recipe.yaml"


def load_recipe_catalog(repository_path: Path) -> dict[str, dict[str, Any]]:
    """Validate legacy Forge entries and Recipe v3 files, indexed by recipe ID."""
    root = repository_path.resolve(strict=True)
    recipes_by_id: dict[str, dict[str, Any]] = {}

    if (root / CATALOG).exists():
        _load_legacy_catalog(root, recipes_by_id)

    recipe_paths = sorted(root.glob(RECIPE_GLOB))
    for recipe_path in recipe_paths:
        _load_recipe_v3(root, recipe_path, recipes_by_id)

    if not recipes_by_id:
        raise ValueError(f"Repository must define {CATALOG} or at least one Recipe v3 file")
    return recipes_by_id


def validate_recipe_catalog(repository_path: Path) -> int:
    """Validate all supported recipe entries and return their count."""
    return len(load_recipe_catalog(repository_path))


def _load_legacy_catalog(root: Path, recipes_by_id: dict[str, dict[str, Any]]) -> None:
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
        recipe_id = _validate_recipe_id(recipe.get("id"), label)
        _ensure_unique_recipe_id(recipes_by_id, recipe_id)

        manifest_path = _repository_file(root, recipe.get("manifest"), f"{label} manifest")
        manifest = _load_yaml(manifest_path)
        _validate_llmisvc_manifest(manifest, manifest_path)
        _validate_model_source(root, recipe.get("model_source"), manifest, label)
        recipes_by_id[recipe_id] = {
            **recipe,
            "recipe_type": "llmisvc",
            "manifest_path": manifest_path,
            "manifest_data": manifest,
            "model_name": manifest["spec"]["model"]["name"],
        }


def _load_recipe_v3(
    root: Path,
    recipe_path: Path,
    recipes_by_id: dict[str, dict[str, Any]],
) -> None:
    recipe = _load_yaml(recipe_path)
    if not isinstance(recipe, dict) or recipe.get("schema_version") != 3:
        raise ValueError(f"{recipe_path} must define schema_version: 3")

    recipe_id = _validate_recipe_id(recipe.get("recipe_id"), str(recipe_path))
    _ensure_unique_recipe_id(recipes_by_id, recipe_id)
    deployment = recipe.get("deployment")
    components = deployment.get("components") if isinstance(deployment, dict) else None
    modelserver = components.get("modelserver") if isinstance(components, dict) else None
    if not isinstance(modelserver, dict) or modelserver.get("kind") != "LeaderWorkerSet":
        raise ValueError(f"{recipe_path} must define a LeaderWorkerSet modelserver component")

    manifest_path = _recipe_file(root, recipe_path.parent, modelserver.get("source"), "modelserver")
    manifest = _load_yaml(manifest_path)
    _validate_kubernetes_manifest(manifest, manifest_path, "LeaderWorkerSet")

    auxiliary_manifests = []
    auxiliary_sources = deployment.get("auxiliary_sources", [])
    if not isinstance(auxiliary_sources, list):
        raise ValueError(f"{recipe_path} deployment.auxiliary_sources must be a list")
    if len(auxiliary_sources) != 1:
        raise ValueError(f"{recipe_path} must define exactly one auxiliary Service")
    for index, source in enumerate(auxiliary_sources, start=1):
        if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
            raise ValueError(f"{recipe_path} auxiliary source {index} must define path and kind")
        source_path = _recipe_file(
            root, recipe_path.parent, source.get("path"), f"auxiliary source {index}"
        )
        source_manifest = _load_yaml(source_path)
        _validate_kubernetes_manifest(source_manifest, source_path, source["kind"])
        if source["kind"] != "Service":
            raise ValueError(f"{source_path} uses unsupported auxiliary kind {source['kind']!r}")
        _validate_service_manifest(source_manifest, source_path)
        auxiliary_manifests.append({"path": source_path, "data": source_manifest})

    model_id = _validate_recipe_id(recipe.get("model_id"), f"{recipe_path} model_id")
    model_path = _repository_file(root, f"models/{model_id}/model.yaml", f"{recipe_path} model")
    model = _load_yaml(model_path)
    if not isinstance(model, dict) or model.get("model_id") != model_id:
        raise ValueError(f"{model_path} must define model_id: {model_id}")
    model_name = model.get("huggingface_id")
    if not isinstance(model_name, str) or not model_name:
        raise ValueError(f"{model_path} must define huggingface_id")
    model_cache = deployment.get("model_cache")
    _validate_lws_model_cache(recipe_path, manifest, model_cache)

    _repository_file(root, recipe.get("hardware_profile"), f"{recipe_path} hardware profile")
    recipes_by_id[recipe_id] = {
        **recipe,
        "recipe_type": "recipe-v3",
        "recipe_path": recipe_path,
        "manifest_path": manifest_path,
        "manifest_data": manifest,
        "auxiliary_manifests": auxiliary_manifests,
        "model_name": model_name,
        "model_cache": model_cache,
    }


def _validate_recipe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", value):
        raise ValueError(f"{label} must define a lowercase recipe ID")
    return value


def _ensure_unique_recipe_id(recipes_by_id: dict[str, Any], recipe_id: str) -> None:
    if recipe_id in recipes_by_id:
        raise ValueError(f"Recipe id must be unique: {recipe_id}")


def _recipe_file(root: Path, recipe_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a recipe-relative file path")
    relative_path = Path(value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"{label} must stay inside its recipe directory")
    path = (recipe_dir / relative_path).resolve()
    if not path.is_relative_to(recipe_dir.resolve()):
        raise ValueError(f"{label} must stay inside its recipe directory")
    return _repository_file(root, str(path.relative_to(root)), label)


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


def _validate_kubernetes_manifest(manifest: Any, path: Path, expected_kind: str) -> None:
    if not isinstance(manifest, dict) or manifest.get("kind") != expected_kind:
        raise ValueError(f"{path} must contain a {expected_kind} mapping")
    metadata = manifest.get("metadata")
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
    ):
        raise ValueError(f"{path} must define metadata.name")


def _validate_service_manifest(manifest: dict[str, Any], path: Path) -> None:
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        raise ValueError(f"{path} must define spec")
    selector = spec.get("selector")
    if not isinstance(selector, dict) or not selector or any(
        not isinstance(key, str) or not key or not isinstance(value, str)
        for key, value in selector.items()
    ):
        raise ValueError(f"{path} must define a non-empty spec.selector")
    ports = spec.get("ports")
    if (
        not isinstance(ports, list)
        or not ports
        or not isinstance(ports[0], dict)
        or type(ports[0].get("port")) is not int
        or not 1 <= ports[0]["port"] <= 65535
    ):
        raise ValueError(f"{path} must define a valid spec.ports[0].port")


def _validate_lws_model_cache(recipe_path: Path, manifest: dict[str, Any], value: Any) -> None:
    label = f"{recipe_path} deployment.model_cache"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must define the LeaderWorkerSet model volume")
    allowed_fields = {"volume_name", "pvc_size", "model_directory_name", "wait_timeout_seconds"}
    required_fields = {"volume_name", "pvc_size", "model_directory_name"}
    missing_fields = required_fields - set(value)
    if missing_fields:
        raise ValueError(f"{label} must define {sorted(missing_fields)}")
    unknown_fields = set(value) - allowed_fields
    if unknown_fields:
        raise ValueError(f"{label} has unsupported fields: {sorted(unknown_fields)}")

    volume_name = value.get("volume_name")
    if not isinstance(volume_name, str) or not re.fullmatch(
        r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", volume_name
    ):
        raise ValueError(f"{label}.volume_name must be a Kubernetes volume name")
    if not isinstance(value["pvc_size"], str) or not value["pvc_size"]:
        raise ValueError(f"{label}.pvc_size must be a non-empty string")
    if not isinstance(value["model_directory_name"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", value["model_directory_name"]
    ):
        raise ValueError(f"{label}.model_directory_name must be a single directory name")
    if "wait_timeout_seconds" in value and (
        type(value["wait_timeout_seconds"]) is not int or value["wait_timeout_seconds"] <= 0
    ):
        raise ValueError(f"{label}.wait_timeout_seconds must be a positive integer")

    spec = manifest.get("spec")
    leader_worker_template = spec.get("leaderWorkerTemplate") if isinstance(spec, dict) else None
    if not isinstance(leader_worker_template, dict):
        raise ValueError(f"{recipe_path} must define spec.leaderWorkerTemplate")
    leader_template = leader_worker_template.get("leaderTemplate")
    if not isinstance(leader_template, dict):
        raise ValueError(f"{recipe_path} must define spec.leaderWorkerTemplate.leaderTemplate")
    pod_templates = [
        ("leaderTemplate", leader_template),
        ("workerTemplate", leader_worker_template.get("workerTemplate")),
    ]

    for template_name, pod_template in pod_templates:
        pod_spec = pod_template.get("spec") if isinstance(pod_template, dict) else None
        if not isinstance(pod_spec, dict):
            raise ValueError(f"{recipe_path} must define {template_name}.spec")
        volumes = pod_spec.get("volumes", [])
        if (
            not isinstance(volumes, list)
            or sum(
                volume.get("name") == volume_name for volume in volumes if isinstance(volume, dict)
            )
            != 1
        ):
            raise ValueError(
                f"{recipe_path} {template_name} must define exactly one volume named {volume_name!r}"
            )
        containers = pod_spec.get("containers", [])
        has_model_mount = False
        if isinstance(containers, list):
            for container in containers:
                mounts = container.get("volumeMounts", []) if isinstance(container, dict) else []
                if isinstance(mounts, list) and any(
                    mount.get("name") == volume_name for mount in mounts if isinstance(mount, dict)
                ):
                    has_model_mount = True
                    break
        if not has_model_mount:
            raise ValueError(f"{recipe_path} {template_name} must mount volume {volume_name!r}")


def _validate_llmisvc_manifest(manifest: Any, path: Path) -> None:
    _validate_kubernetes_manifest(manifest, path, "LLMInferenceService")
    if manifest.get("apiVersion") != "serving.kserve.io/v1alpha2":
        raise ValueError(f"{path} must use serving.kserve.io/v1alpha2")
    model = (
        manifest.get("spec", {}).get("model") if isinstance(manifest.get("spec"), dict) else None
    )
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
    if not pvc_name or not _mentions(guide, source_uri) or not _mentions(guide, pvc_name):
        raise ValueError(f"{label} preparation guide must document {source_uri} and PVC {pvc_name}")
    if not _contains_pvc_claim(manifest, pvc_name):
        raise ValueError(f"{label} manifest must mount PVC {pvc_name}")
    storage_initializer = manifest["spec"].get("storageInitializer")
    if not isinstance(storage_initializer, dict) or storage_initializer.get("enabled") is not False:
        raise ValueError(f"{label} PVC-backed manifest must disable the storage initializer")


def _mentions(text: str, token: str) -> bool:
    return re.search(rf"(?<![\w./:-]){re.escape(token)}(?![\w./:-])", text) is not None


def _contains_pvc_claim(value: Any, pvc_name: str) -> bool:
    if isinstance(value, dict):
        claim = value.get("persistentVolumeClaim")
        if isinstance(claim, dict) and claim.get("claimName") == pvc_name:
            return True
        return any(_contains_pvc_claim(child, pvc_name) for child in value.values())
    if isinstance(value, list):
        return any(_contains_pvc_claim(child, pvc_name) for child in value)
    return False
