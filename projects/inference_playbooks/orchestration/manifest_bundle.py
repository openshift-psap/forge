"""The prospective recipe launch boundary: one complete customer platform bundle."""

import copy
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import yaml

PRIMARY_KINDS = {"Deployment", "InferenceService", "LeaderWorkerSet", "LLMInferenceService"}
RESOURCE_TYPES = {
    "Deployment": ("apps/v1", "deployments"),
    "InferenceService": ("serving.kserve.io/v1beta1", "inferenceservices"),
    "LeaderWorkerSet": ("leaderworkerset.x-k8s.io/v1", "leaderworkersets"),
    "LLMInferenceService": ("serving.kserve.io/v1alpha2", "llminferenceservices"),
    "ServingRuntime": ("serving.kserve.io/v1alpha1", "servingruntimes"),
}


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ValueError("YAML requires unique string mapping keys")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class _MetadataLoader(_UniqueLoader):
    """Recipe/model/hardware dates are strings, as in playbooks' UniqueKeyLoader."""


_MetadataLoader.add_constructor(
    "tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node)
)


def load_metadata_document(raw: bytes) -> dict:
    """Parse metadata without weakening the separate workload-document boundary."""
    try:
        documents = [doc for doc in yaml.load_all(raw, Loader=_MetadataLoader) if doc is not None]
        _json_value(documents)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError(
            "Invalid metadata YAML: require unique keys and supported safe values"
        ) from None
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("Metadata YAML must contain exactly one mapping")
    return documents[0]


def load_documents(raw: bytes) -> list[dict]:
    """Never include parser snippets (which can contain credentials) in errors."""
    try:
        documents = [doc for doc in yaml.load_all(raw, Loader=_UniqueLoader) if doc is not None]
        _json_value(documents)
        return documents
    except (yaml.YAMLError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError(
            "Invalid manifest YAML: require unique keys and JSON-compatible values"
        ) from None


def _json_value(value):
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for child in value:
            _json_value(child)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for child in value.values():
            _json_value(child)
        return
    raise ValueError("Manifest must contain JSON-compatible values")


def classify_documents(documents: list[dict]) -> dict:
    primary = []
    runtimes = []
    identities = set()
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("Every manifest document must be a Kubernetes object")
        kind = document.get("kind")
        if not isinstance(kind, str) or kind not in RESOURCE_TYPES:
            # Do not print an unsupported document or its potentially sensitive payload.
            raise ValueError(
                "Unsupported manifest kind; authored Services, Secrets and Lists are forbidden"
            )
        if document.get("apiVersion") != RESOURCE_TYPES[kind][0]:
            raise ValueError(f"Unsupported apiVersion for {kind}")
        metadata = document.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*", name
        ):
            raise ValueError("Every manifest document must define a valid metadata.name")
        if len(name) > 253 or "generateName" in metadata:
            raise ValueError("Manifest must use a fixed valid metadata.name, not generateName")
        namespace = metadata.get("namespace")
        if "namespace" in metadata and (not isinstance(namespace, str) or not namespace):
            raise ValueError("An explicit metadata.namespace must be nonempty")
        identity = (kind, namespace, name)
        if identity in identities:
            raise ValueError("Duplicate manifest object identity")
        identities.add(identity)
        if kind in PRIMARY_KINDS:
            primary.append(document)
        else:
            runtimes.append(document)
    if len(primary) != 1:
        raise ValueError(
            "Bundle must contain exactly one primary resource; multi-endpoint bundles are unsupported"
        )
    if len(runtimes) > 1 or (runtimes and primary[0]["kind"] != "InferenceService"):
        raise ValueError("Only InferenceService accepts one namespaced ServingRuntime companion")
    return {"primary": primary[0], "servingruntime": runtimes[0] if runtimes else None}


def normalized_documents(documents: list[dict], namespace: str) -> list[dict]:
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace)
        or len(namespace) > 63
    ):
        raise ValueError("Recipe launch requires a valid selected namespace")
    classify_documents(documents)
    submitted = copy.deepcopy(documents)
    for document in submitted:
        metadata = document["metadata"]
        if "namespace" not in metadata:
            metadata["namespace"] = namespace
        elif metadata["namespace"] != namespace:
            raise ValueError("Explicit source namespace does not match the selected namespace")
    classify_documents(submitted)  # Includes identities that collide only after placement.
    return submitted


def assert_submission(source: list[dict], submitted: list[dict], namespace: str) -> None:
    expected = normalized_documents(source, namespace)
    _json_value(submitted)

    # JSON comparison preserves scalar types (Python equality considers True == 1).
    def canonical(value):
        return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))

    if canonical(expected) != canonical(submitted):
        raise ValueError("Submitted bundle differs from source beyond absent-namespace placement")


def _revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", result):
        raise ValueError("Bundle evidence requires an actual full Git checkout revision")
    return result


def _git_bytes(root: Path, revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), "show", f"{revision}:{path}"], capture_output=True, check=True
    ).stdout


def _git_artifact_paths(root: Path, revision: str, folder: Path) -> list[Path]:
    names = (
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-tree",
                "--name-only",
                "-z",
                f"{revision}:{folder.relative_to(root).as_posix()}",
            ],
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    return sorted(
        folder / name for name in names if name and Path(name).suffix in {".yaml", ".yml"}
    )


def load_source_bundle(root: Path, recipe: dict, selection: Path | None = None) -> dict:
    if selection is None:
        raise ValueError(
            "An explicit manifest_path is required; platform selection cannot be inferred"
        )
    root = root.resolve(strict=True)
    recipe_path = Path(recipe["recipe_path"])
    if recipe_path != recipe_path.resolve(strict=True) or not recipe_path.is_relative_to(root):
        raise ValueError("Recipe source must be canonical and contained in the checkout")
    relative = recipe_path.relative_to(root)
    if (
        len(relative.parts) != 5
        or relative.parts[0] != "models"
        or relative.parts[2] != "recipes"
        or relative.name != "recipe.yaml"
    ):
        raise ValueError("Strict certification requires a canonical flat v4 recipe")
    recipe_raw = recipe_path.read_bytes()
    authored = load_metadata_document(recipe_raw)
    if authored.get("schema_version") != 4 or authored.get("recipe_id") != recipe.get("recipe_id"):
        raise ValueError("Strict certification requires the selected v4 recipe identity")
    platforms = authored.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        raise ValueError("Recipe must declare a platform")
    folders = {}
    for platform in platforms:
        if not isinstance(platform, dict) or any(
            not isinstance(platform.get(key), str)
            or not re.fullmatch(r"[A-Za-z0-9._-]+", platform[key])
            for key in ("stack", "version")
        ):
            raise ValueError("Recipe platform must define stack and version")
        folder = recipe_path.parent / "manifests" / f"{platform['stack']}-{platform['version']}"
        if folder in folders:
            raise ValueError("Duplicate recipe platform")
        folders[folder] = platform
    try:
        canonical_selection = selection.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(
            "manifest_path must identify an existing selected platform artifact"
        ) from None
    if selection != canonical_selection or selection.parent not in folders:
        raise ValueError("manifest_path must identify a canonical selected platform artifact")
    folder = selection.parent
    platform = folders[folder]
    if platform.get("blocked"):
        raise ValueError("Selected platform is blocked")
    if not folder.is_dir() or folder != folder.resolve(strict=True):
        raise ValueError("Selected platform artifact directory must be canonical")
    # Matches tools/catalog.py:artifacts_for: immediate YAML/YML only, not recursive.
    paths = sorted(path for path in folder.glob("*") if path.suffix in {".yaml", ".yml"})
    if not paths or selection not in paths:
        raise ValueError("Selected platform has no matching customer artifacts")
    revision = _revision(root)
    recipe_relative = relative.as_posix()
    if recipe_raw != _git_bytes(root, revision, recipe_relative):
        raise ValueError("Recipe differs from its tested Git revision")
    if paths != _git_artifact_paths(root, revision, folder):
        raise ValueError("Customer artifact inventory differs from the tested Git revision")
    documents = []
    source_files = []
    seen = set()
    for path in paths:
        resolved = path.resolve(strict=True)
        if path != resolved or not path.is_file() or resolved in seen:
            raise ValueError("Source artifacts must be unique canonical files")
        seen.add(resolved)
        raw = path.read_bytes()
        relative_path = path.relative_to(root).as_posix()
        if raw != _git_bytes(root, revision, relative_path):
            raise ValueError("Source artifact differs from its tested Git revision")
        parsed = load_documents(raw)
        if not parsed:
            raise ValueError("Customer artifact must contain nonempty object documents")
        documents.extend(parsed)
        source_files.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "document_count": len(parsed),
            }
        )
    bundle = classify_documents(documents)
    return {
        **bundle,
        "documents": documents,
        "source_files": source_files,
        "recipe_id": authored["recipe_id"],
        "recipe_path": recipe_relative,
        "recipe_revision": revision,
        "platform": platform,
    }


def write_bundle_artifacts(
    bundle: dict, submitted: list[dict], namespace: str, src: Path, forge_root: Path
) -> dict:
    assert_submission(bundle["documents"], submitted, namespace)
    source_bytes = yaml.safe_dump_all(bundle["documents"], sort_keys=False).encode("utf-8")
    submitted_bytes = yaml.safe_dump_all(submitted, sort_keys=False).encode("utf-8")
    metadata = {
        "contract_version": 1,
        "recipe_id": bundle["recipe_id"],
        "recipe_path": bundle["recipe_path"],
        "recipe_revision": bundle["recipe_revision"],
        "forge_revision": _revision(forge_root),
        "target_namespace": namespace,
        "source_files": bundle["source_files"],
        "source_bundle_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "submitted_bundle_sha256": hashlib.sha256(submitted_bytes).hexdigest(),
    }
    src.mkdir(parents=True, exist_ok=True)
    (src / "source-bundle.yaml").write_bytes(source_bytes)
    (src / "submitted-bundle.yaml").write_bytes(submitted_bytes)
    (src / "bundle-metadata.yaml").write_text(
        yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
    )
    return metadata


def checked_submission(src: Path, namespace: str, expected_metadata: dict) -> list[dict]:
    """Read the actual bytes at the final request boundary, not a prior snapshot."""
    metadata_documents = load_documents((src / "bundle-metadata.yaml").read_bytes())
    if len(metadata_documents) != 1 or not isinstance(metadata_documents[0], dict):
        raise ValueError("Bundle metadata must contain one mapping")
    metadata = metadata_documents[0]
    if (
        json.dumps(metadata, sort_keys=True) != json.dumps(expected_metadata, sort_keys=True)
        or metadata["target_namespace"] != namespace
    ):
        raise ValueError("Bundle metadata changed before submission")
    source_raw = (src / "source-bundle.yaml").read_bytes()
    submitted_raw = (src / "submitted-bundle.yaml").read_bytes()
    if (
        hashlib.sha256(source_raw).hexdigest() != metadata["source_bundle_sha256"]
        or hashlib.sha256(submitted_raw).hexdigest() != metadata["submitted_bundle_sha256"]
    ):
        raise ValueError("Bundle artifact hash mismatch before submission")
    source = load_documents(source_raw)
    submitted = load_documents(submitted_raw)
    assert_submission(source, submitted, namespace)
    return submitted
