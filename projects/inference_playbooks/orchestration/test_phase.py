import copy
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yaml

from projects.core.dsl.utils import slugify_identifier
from projects.core.dsl.utils.k8s import oc, oc_get_json
from projects.core.library import config, env, vault
from projects.core.library.postprocess import (
    create_test_metadata,
    run_and_postprocess,
    update_test_labels_with_status,
    update_test_labels_with_timing,
)
from projects.foreign_testing.library import initialize as foreign_repository
from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark import main as run_guidellm_benchmark
from projects.inference_playbooks.orchestration.recipe_validation import load_recipe_catalog
from projects.kserve.toolbox.capture_llmisvc_state import main as capture_llmisvc_state
from projects.kserve.toolbox.capture_lws_state import main as capture_lws_state
from projects.kserve.toolbox.deploy_llmisvc import main as deploy_llmisvc
from projects.kserve.toolbox.deploy_lws import main as deploy_lws
from projects.kserve.toolbox.prepare_hf_model_cache import main as prepare_hf_model_cache
from projects.kserve.toolbox.prepare_hf_model_cache.utils import build_model_cache_spec

logger = logging.getLogger(__name__)
PROJECTS_DIR = Path(__file__).resolve().parents[2]
RHAIIS_CONFIG = PROJECTS_DIR / "rhaiis/orchestration/config.yaml"
RHAIIS_WORKLOADS = PROJECTS_DIR / "rhaiis/orchestration/config.d/workloads.yaml"
KPI_PLATFORM = "OCP"
KPI_TEST_HARNESS = "forge-inference-playbooks"


def test():
    """Run the Inference Playbooks test with outcome postprocessing."""
    recipe_id = config.project.get_config("inference_playbooks.recipe")
    postprocess_enabled = (
        bool(config.project.get_config("caliper.postprocess.enabled"))
        and bool(recipe_id)
        and not _clusterless_mode()
    )
    if not postprocess_enabled:
        config.project.set_config("caliper.postprocess.enabled", False)
    return run_and_postprocess(do_test)


def create_custom_test_metadata(recipe_id: str | None = None):
    workload_keys = None
    if recipe_id:
        workload_configs = _get_workload_configs()
        workload_keys = [k for k, _ in workload_configs]
    workload_label = ",".join(workload_keys) if workload_keys else None
    labels = {
        "project": "inference-playbooks",
        "validation": f"recipe-{workload_label}" if recipe_id else "recipe-catalog-validation",
    }
    if recipe_id:
        labels["recipe_id"] = recipe_id
        labels["workload_key"] = workload_label
        labels["skip"] = True
    test_dir = env.ARTIFACT_DIR
    create_test_metadata(test_dir, labels)
    return test_dir


def _clusterless_mode() -> bool:
    return os.environ.get("CLUSTERLESS_MODE", "").lower() == "true" or not os.environ.get(
        "KUBECONFIG"
    )


def do_test():
    logger.info("=== Inference Playbooks Project Test Phase ===")
    repository_path = foreign_repository.initialize()
    recipe_id = config.project.get_config("inference_playbooks.recipe")
    if recipe_id is not None and not isinstance(recipe_id, str):
        raise ValueError("inference_playbooks.recipe must be a recipe ID string")

    manifest_path = config.project.get_config("inference_playbooks.manifest_path")
    if manifest_path is not None:
        if not isinstance(manifest_path, str) or not manifest_path.strip():
            raise ValueError("inference_playbooks.manifest_path must be a non-empty path")
        if recipe_id is None:
            raise ValueError(
                "A recipe ID is required when inference_playbooks.manifest_path is set"
            )
        recipes = {recipe_id: _load_recipe_metadata(repository_path, recipe_id)}
        manifest_bundle = _load_manifest_override(
            repository_path, manifest_path, recipes[recipe_id]
        )
    else:
        recipes = load_recipe_catalog(repository_path)
        manifest_bundle = None

    logger.info("Loaded metadata for %d Inference Playbooks recipe(s)", len(recipes))
    if manifest_bundle is not None:
        logger.info("Using manifest override %s", manifest_bundle["manifest_path"])

    with env.NextArtifactDir("inference_playbooks_test_dir"):
        test_dir = create_custom_test_metadata(recipe_id)
        try:
            update_test_labels_with_timing(test_dir, "test", "start")
            if recipe_id is None:
                logger.info("No recipe selected; validated recipes without launching a service")
            elif recipe_id not in recipes:
                available = ", ".join(sorted(recipes))
                raise ValueError(f"Unknown recipe ID {recipe_id!r}; available recipes: {available}")
            else:
                _launch_recipe(recipe_id, recipes[recipe_id], manifest_bundle)
        except Exception as exc:
            logger.exception("Test failed with exception")
            update_test_labels_with_status(test_dir, False, f"Test failed: {exc}")
            raise
        finally:
            update_test_labels_with_timing(test_dir, "test", "end")

    update_test_labels_with_status(test_dir, True, "Test completed successfully")
    return 0


def _load_recipe_metadata(repository_path: Path, recipe_id: str) -> dict:
    root = repository_path.resolve(strict=True)
    matches = []
    for path in root.glob("models/**/recipe.yaml"):
        recipe_data = _load_yaml_document(path)
        if isinstance(recipe_data, dict) and recipe_data.get("recipe_id") == recipe_id:
            matches.append((path, recipe_data))

    if not matches:
        raise ValueError(
            f"Unknown Inference Playbooks recipe ID {recipe_id!r}; no matching recipe.yaml found"
        )
    if len(matches) != 1:
        raise ValueError(
            f"Recipe ID {recipe_id!r} must be unique in the Inference Playbooks checkout"
        )

    recipe_path, recipe_data = matches[0]
    model_id = recipe_data.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise ValueError(f"{recipe_path} must define model_id")
    model_path = _repository_file(root, f"models/{model_id}/model.yaml", f"{recipe_path} model")
    model_data = _load_yaml_document(model_path)
    model_name = model_data.get("huggingface_id") if isinstance(model_data, dict) else None
    if not isinstance(model_name, str) or not model_name:
        serving = recipe_data.get("serving")
        model_name = serving.get("model") if isinstance(serving, dict) else None
    if not isinstance(model_name, str) or not model_name:
        raise ValueError(f"{recipe_path} needs serving.model or model.yaml huggingface_id")

    hardware_profile_data = {}
    hardware_profile = recipe_data.get("hardware_profile")
    if hardware_profile is not None:
        hardware_path = _repository_file(root, hardware_profile, f"{recipe_path} hardware profile")
        hardware_profile_data = _load_yaml_document(hardware_path)
        if not isinstance(hardware_profile_data, dict):
            raise ValueError(f"{hardware_path} must define a mapping")

    platforms = recipe_data.get("platforms")
    platform = recipe_data.get("platform")
    if not isinstance(platform, dict) and isinstance(platforms, list) and platforms:
        first_platform = platforms[0]
        if isinstance(first_platform, dict):
            platform = {"version": first_platform.get("version")}

    deployment = recipe_data.get("deployment")
    model_cache = (
        deployment.get("model_cache")
        if isinstance(deployment, dict)
        else recipe_data.get("model_cache")
    )
    serving = recipe_data.get("serving")
    served_model_name = (
        serving.get("served_model_name") or serving.get("model")
        if isinstance(serving, dict)
        else None
    ) or model_name

    return {
        **recipe_data,
        "recipe_path": recipe_path,
        "model_name": model_name,
        "served_model_name": served_model_name,
        "model_cache": model_cache,
        "hardware_profile_data": hardware_profile_data,
        "platform": platform,
    }


def _repository_file(
    root: Path,
    value: str,
    label: str,
    recipe_path: Path | None = None,
) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty path")
    relative_path = Path(value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"{label} must stay inside the Inference Playbooks checkout")

    candidates = [root / relative_path]
    if recipe_path is not None:
        candidates.append(recipe_path.parent / relative_path)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(root):
            raise ValueError(f"{label} must stay inside the Inference Playbooks checkout")
        if resolved.is_file():
            return resolved
    raise ValueError(f"{label} does not exist in the Inference Playbooks checkout: {value}")


def _load_yaml_document(path: Path):
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}") from exc


def _load_manifest_override(
    repository_path: Path,
    manifest_path: str,
    recipe: dict,
) -> dict:
    root = repository_path.resolve(strict=True)
    manifest_path = _repository_file(
        root,
        manifest_path,
        "inference_playbooks.manifest_path",
        recipe.get("recipe_path"),
    )
    try:
        with manifest_path.open(encoding="utf-8") as stream:
            documents = [doc for doc in yaml.safe_load_all(stream) if doc is not None]
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid manifest override YAML in {manifest_path}") from exc
    bundle = _classify_manifest_documents(documents)
    bundle["manifest_path"] = manifest_path
    return bundle


def _classify_manifest_documents(documents: list[dict]) -> dict:
    primary_kinds = {"Deployment", "InferenceService", "LeaderWorkerSet", "LLMInferenceService"}
    companion_kinds = {"Service", "ServingRuntime"}
    primary = []
    companions = {}
    for document in documents:
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("apiVersion"), str)
            or not isinstance(document.get("kind"), str)
        ):
            raise ValueError("Every manifest override document must define apiVersion and kind")
        metadata = document.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
            raise ValueError("Every manifest override document must define metadata.name")
        if not metadata["name"]:
            raise ValueError("Every manifest override document must define metadata.name")
        kind = document["kind"]
        if kind in primary_kinds:
            primary.append(document)
        elif kind in companion_kinds:
            if kind in companions:
                raise ValueError(f"Manifest override contains more than one {kind}")
            companions[kind] = document
        else:
            raise ValueError(f"Unsupported manifest override kind {kind!r}")

    if len(primary) != 1:
        if len(primary) > 1 and all(doc["kind"] == "Deployment" for doc in primary):
            raise ValueError(
                "Manifest override contains multiple Deployments; P/D recipes need an llm-d "
                "router and are not supported by this single-endpoint launch flow"
            )
        raise ValueError("Manifest override must contain exactly one primary resource")

    return {
        "primary": primary[0],
        "service": companions.get("Service"),
        "servingruntime": companions.get("ServingRuntime"),
    }


def _manifest_bundle_from_recipe(recipe: dict) -> dict:
    documents = [recipe["manifest_data"]]
    documents.extend(source["data"] for source in recipe.get("auxiliary_manifests", []))
    return _classify_manifest_documents(documents)


def _launch_recipe(recipe_id: str, recipe: dict, manifest_bundle: dict | None = None) -> None:
    if _clusterless_mode():
        raise RuntimeError(
            "A recipe launch needs a target cluster; rerun the PR test with /cluster <registered-target>"
        )

    bundle = manifest_bundle or _manifest_bundle_from_recipe(recipe)
    manifest = bundle["primary"]
    namespace = config.project.get_config("inference_playbooks.namespace") or manifest.get(
        "metadata", {}
    ).get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError(
            "Recipe launch needs a namespace; set /var inference_playbooks.namespace: <namespace>"
        )
    if slugify_identifier(namespace) != namespace:
        raise ValueError(f"Invalid Kubernetes namespace: {namespace!r}")

    kind = manifest.get("kind")
    if kind == "LLMInferenceService":
        if bundle["service"] or bundle["servingruntime"]:
            raise ValueError("LLMInferenceService overrides do not accept companion resources")
        _launch_llmisvc_recipe(recipe_id, recipe, manifest, namespace)
    elif kind == "LeaderWorkerSet":
        if bundle["servingruntime"]:
            raise ValueError("LeaderWorkerSet overrides do not accept a ServingRuntime")
        _launch_lws_recipe(recipe_id, recipe, manifest, bundle["service"], namespace)
    elif kind == "Deployment":
        if bundle["servingruntime"]:
            raise ValueError("Deployment overrides do not accept a ServingRuntime")
        _launch_deployment_recipe(recipe_id, recipe, manifest, bundle["service"], namespace)
    elif kind == "InferenceService":
        _launch_isvc_recipe(
            recipe_id, recipe, manifest, bundle["servingruntime"], bundle["service"], namespace
        )
    else:
        raise ValueError(f"Recipe {recipe_id!r} has unsupported manifest kind {kind!r}")


def _launch_llmisvc_recipe(
    recipe_id: str,
    recipe: dict,
    manifest_data: dict,
    namespace: str,
) -> None:
    manifest = copy.deepcopy(manifest_data)
    metadata = manifest["metadata"]
    original_name = metadata["name"]
    if slugify_identifier(original_name) != original_name:
        raise ValueError(f"Recipe has an invalid Kubernetes service name: {original_name!r}")
    service_name = _unique_name(original_name)
    manifest["metadata"] = {**metadata, "name": service_name, "namespace": namespace}

    def deploy() -> str:
        _prepare_llmisvc_model(recipe_id, recipe, manifest, namespace)
        manifest_path = _write_launch_manifest(service_name, manifest)
        return deploy_llmisvc.run(
            namespace=namespace,
            inference_service_manifest_path=str(manifest_path),
            gateway_status_address_name=None,
            deploy_monitor=False,
        )

    def capture() -> None:
        capture_llmisvc_state.run(llmisvc_name=service_name, namespace=namespace)

    def cleanup() -> None:
        _delete_resources(namespace, [("llminferenceservice", service_name)])

    _deploy_benchmark_finalize(recipe_id, recipe, namespace, service_name, deploy, capture, cleanup)


def _prepare_llmisvc_model(recipe_id: str, recipe: dict, manifest: dict, namespace: str) -> None:
    model = manifest.get("spec", {}).get("model")
    model_uri = model.get("uri") if isinstance(model, dict) else None
    if not isinstance(model_uri, str) or not model_uri:
        raise ValueError("LLMInferenceService override must define spec.model.uri")
    if model_uri.startswith("hf://"):
        cache_spec = _prepare_hf_model_cache(recipe_id, model_uri, namespace)
        manifest["spec"]["model"]["uri"] = cache_spec["model_uri"]
        manifest["spec"]["storageInitializer"] = {"enabled": False}
        logger.info("Prepared model cache %s for %s", cache_spec["pvc_name"], model_uri)
        return

    if not model_uri.startswith("pvc://"):
        raise ValueError(f"Unsupported LLMInferenceService model URI: {model_uri!r}")
    model_source = recipe.get("model_source", {})
    pvc_name = model_uri.removeprefix("pvc://").split("/", maxsplit=1)[0]
    if not pvc_name:
        raise ValueError("LLMInferenceService PVC model URI must name a PVC")
    result = oc(
        "get", "pvc", pvc_name, "-n", namespace, "-o", "json", check=False, log_stdout=False
    )
    if result.returncode != 0:
        source_uri = model_source.get("source_uri")
        source_note = f" from {source_uri}" if source_uri else ""
        raise RuntimeError(
            f"Recipe model URI {model_uri!r} requires an existing populated PVC in "
            f"namespace {namespace!r}{source_note}"
        )
    pvc = json.loads(result.stdout)
    if pvc.get("status", {}).get("phase") != "Bound":
        raise RuntimeError(f"Recipe PVC {pvc_name!r} in namespace {namespace!r} is not Bound")


def _prepare_hf_model_cache(
    model_key: str,
    model_uri: str,
    namespace: str,
    *,
    model_cache: dict | None = None,
) -> dict:
    cache = config.project.get_config("model_cache")
    pvc = cache["pvc"]
    download = cache["download"]
    overrides = model_cache or {}
    pvc_size = overrides.get("pvc_size", pvc["size"])
    model_directory_name = overrides.get("model_directory_name", pvc["model_directory_name"])
    wait_timeout_seconds = overrides.get("wait_timeout_seconds", download["wait_timeout_seconds"])
    cache_spec = build_model_cache_spec(
        namespace=namespace,
        model_key=model_key,
        model_uri=model_uri,
        pvc_size=pvc_size,
        access_mode=pvc["access_mode"],
        storage_class_name=pvc["storage_class_name"],
        pvc_name_prefix=pvc["name_prefix"],
        model_directory_name=model_directory_name,
        marker_filename=cache["marker_filename"],
    )
    hf_token_file = vault.get_vault_content_path("psap-forge-hf", "hf_token")
    if hf_token_file is None:
        logger.warning("HF vault is unavailable; attempting the public model download")
    prepare_hf_model_cache.run(
        namespace=namespace,
        model_key=model_key,
        model_uri=model_uri,
        pvc_size=pvc_size,
        access_mode=pvc["access_mode"],
        storage_class_name=pvc["storage_class_name"],
        pvc_name_prefix=pvc["name_prefix"],
        model_directory_name=model_directory_name,
        marker_filename=cache["marker_filename"],
        wait_timeout_seconds=wait_timeout_seconds,
        poll_interval_seconds=download["poll_interval_seconds"],
        downloader_image=cache["hf"]["downloader_image"],
        hf_token_file_path=hf_token_file,
        pod_image_pull_policy=download["pod_image_pull_policy"],
    )
    return cache_spec


def _launch_lws_recipe(
    recipe_id: str,
    recipe: dict,
    manifest_data: dict,
    service_data: dict | None,
    namespace: str,
) -> None:
    lws = copy.deepcopy(manifest_data)
    service = copy.deepcopy(service_data) if service_data is not None else None
    original_name = lws["metadata"]["name"]
    if slugify_identifier(original_name) != original_name:
        raise ValueError(f"Recipe has an invalid LeaderWorkerSet name: {original_name!r}")
    lws_name = _unique_name(original_name)
    service_name = None
    run_label = lws_name
    endpoint_label = "leader"
    lws_metadata = lws["metadata"]
    lws_labels = lws_metadata.get("labels") or {}
    if not isinstance(lws_labels, dict):
        raise ValueError(f"Recipe {recipe_id!r} LeaderWorkerSet metadata.labels must be a mapping")
    lws["metadata"] = {
        **lws_metadata,
        "name": lws_name,
        "namespace": namespace,
        "labels": {**lws_labels, "forge.openshift.io/run": run_label},
    }
    spec = lws.get("spec")
    templates = spec.get("leaderWorkerTemplate") if isinstance(spec, dict) else None
    if not isinstance(templates, dict):
        raise ValueError(f"Recipe {recipe_id!r} must define spec.leaderWorkerTemplate")
    leader_template = templates.get("leaderTemplate")
    if not isinstance(leader_template, dict):
        raise ValueError(f"Recipe {recipe_id!r} must define an explicit leaderTemplate")
    leader_metadata = leader_template.setdefault("metadata", {})
    if not isinstance(leader_metadata, dict):
        raise ValueError(f"Recipe {recipe_id!r} leaderTemplate metadata must be a mapping")
    leader_labels = leader_metadata.setdefault("labels", {})
    if not isinstance(leader_labels, dict):
        raise ValueError(f"Recipe {recipe_id!r} leaderTemplate metadata.labels must be a mapping")
    leader_labels.update(
        {"forge.openshift.io/run": run_label, "forge.openshift.io/endpoint": endpoint_label}
    )
    worker_template = templates.get("workerTemplate")
    if not isinstance(worker_template, dict):
        raise ValueError(f"Recipe {recipe_id!r} must define an explicit workerTemplate")
    worker_metadata = worker_template.setdefault("metadata", {})
    if not isinstance(worker_metadata, dict):
        raise ValueError(f"Recipe {recipe_id!r} workerTemplate metadata must be a mapping")
    worker_labels = worker_metadata.setdefault("labels", {})
    if not isinstance(worker_labels, dict):
        raise ValueError(f"Recipe {recipe_id!r} workerTemplate metadata.labels must be a mapping")
    worker_labels["forge.openshift.io/run"] = run_label

    if service is not None:
        service_metadata = service["metadata"]
        original_service_name = service_metadata["name"]
        service_name = _unique_name(original_service_name)
        service_labels = service_metadata.get("labels") or {}
        if not isinstance(service_labels, dict):
            raise ValueError(f"Recipe {recipe_id!r} Service metadata.labels must be a mapping")
        service["metadata"] = {
            **service_metadata,
            "name": service_name,
            "namespace": namespace,
            "labels": {**service_labels, "forge.openshift.io/run": run_label},
        }
        service_spec = service.get("spec")
        if not isinstance(service_spec, dict):
            raise ValueError(f"Recipe {recipe_id!r} Service must define spec")
        service_spec["selector"] = {
            "forge.openshift.io/run": run_label,
            "forge.openshift.io/endpoint": endpoint_label,
        }

    for template_key in ("leaderTemplate", "workerTemplate"):
        template = templates.get(template_key, {})
        template_spec = template.get("spec")
        if isinstance(template_spec, dict):
            _strip_hardcoded_uid(template_spec)

    rdma_resource = config.project.get_config("inference_playbooks.rdma_resource")
    if rdma_resource:
        deploy_lws.replace_resource_name(lws, "rdma/ib", rdma_resource)
    lws_ready_timeout_seconds = config.project.get_config(
        "inference_playbooks.lws_ready_timeout_seconds"
    )
    pod_selector = f"forge.openshift.io/run={run_label}"

    def deploy() -> str:
        nonlocal service_name
        if recipe.get("model_cache"):
            _prepare_lws_model_cache(recipe, lws, namespace)
        service_path = (
            _write_launch_manifest(f"{service_name}-service", service)
            if service is not None
            else None
        )
        lws_path = _write_launch_manifest(f"{lws_name}-lws", lws)
        endpoint_url = deploy_lws.run(
            namespace=namespace,
            service_manifest_path=str(service_path) if service_path else None,
            leader_worker_set_manifest_path=str(lws_path),
            pod_selector=pod_selector,
            timeout_seconds=lws_ready_timeout_seconds,
        )
        if service_name is None:
            hostname = urlparse(endpoint_url).hostname
            if not hostname:
                raise RuntimeError("LeaderWorkerSet endpoint discovery returned an invalid URL")
            service_name = hostname.split(".", maxsplit=1)[0]
        return endpoint_url

    def capture() -> None:
        capture_lws_state.run(
            namespace=namespace,
            lws_name=lws_name,
            service_name=service_name,
            pod_selector=pod_selector,
        )

    def cleanup() -> None:
        resources = [("leaderworkerset", lws_name)]
        if service_name:
            resources.append(("service", service_name))
        _delete_resources(namespace, resources)

    _deploy_benchmark_finalize(recipe_id, recipe, namespace, lws_name, deploy, capture, cleanup)


def _prepare_lws_model_cache(recipe: dict, lws: dict, namespace: str) -> None:
    model_cache = recipe["model_cache"]
    model_uri = f"hf://{recipe['model_name']}"
    cache_spec = _prepare_hf_model_cache(
        recipe["model_id"],
        model_uri,
        namespace,
        model_cache=model_cache,
    )
    deploy_lws.attach_model_cache_pvc(lws, model_cache["volume_name"], cache_spec["pvc_name"])
    logger.info("Prepared model cache %s for %s", cache_spec["pvc_name"], model_uri)


def _strip_hardcoded_uid(pod_spec: dict) -> None:
    """Remove hardcoded runAsUser and fix env for OpenShift SCC compatibility.

    OpenShift assigns UIDs from the namespace range. A hardcoded UID like
    65534 is rejected by the restricted SCC. Removing it lets OpenShift
    assign a valid UID while preserving runAsNonRoot and container-level
    security settings.

    The assigned UID has no /etc/passwd entry, so getpass.getuser() and
    PyTorch cache_dir fail. Setting HOME and USER on each container
    prevents this.
    """
    security_context = pod_spec.get("securityContext")
    if isinstance(security_context, dict) and "runAsUser" in security_context:
        logger.info(
            "Removing hardcoded runAsUser=%s for OpenShift SCC compatibility",
            security_context["runAsUser"],
        )
        del security_context["runAsUser"]

    ocp_env = [
        {"name": "HOME", "value": "/tmp"},
        {"name": "USER", "value": "nobody"},
    ]
    for container in pod_spec.get("containers", []):
        env_list = container.setdefault("env", [])
        existing_names = {e["name"] for e in env_list if isinstance(e, dict)}
        for entry in ocp_env:
            if entry["name"] not in existing_names:
                env_list.append(entry)


def _launch_deployment_recipe(
    recipe_id: str,
    recipe: dict,
    manifest_data: dict,
    service_data: dict | None,
    namespace: str,
) -> None:
    deployment = copy.deepcopy(manifest_data)
    metadata = deployment["metadata"]
    original_name = metadata["name"]
    if slugify_identifier(original_name) != original_name:
        raise ValueError(f"Recipe has an invalid Deployment name: {original_name!r}")

    deployment_name = _unique_name(original_name)
    run_label = deployment_name
    run_label_key = "forge.openshift.io/run"
    spec = deployment.get("spec")
    if not isinstance(spec, dict):
        raise ValueError(f"Recipe {recipe_id!r} Deployment must define spec")
    selector_spec = spec.get("selector")
    selector = selector_spec.get("matchLabels") if isinstance(selector_spec, dict) else None
    pod_template = spec.get("template")
    if not isinstance(pod_template, dict):
        raise ValueError(f"Recipe {recipe_id!r} Deployment must define spec.template")
    pod_metadata = pod_template.setdefault("metadata", {})
    if not isinstance(pod_metadata, dict):
        raise ValueError(f"Recipe {recipe_id!r} Deployment pod metadata must be a mapping")
    pod_labels = pod_metadata.setdefault("labels", {})
    pod_spec = pod_template.get("spec")
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    if (
        not isinstance(selector, dict)
        or not selector
        or not isinstance(pod_labels, dict)
        or not isinstance(containers, list)
        or not containers
    ):
        raise ValueError(
            f"Recipe {recipe_id!r} Deployment needs containers and matching pod labels"
        )
    if any(pod_labels.get(key) != value for key, value in selector.items()):
        raise ValueError(f"Recipe {recipe_id!r} Deployment selector does not match pod labels")
    original_pod_labels = dict(pod_labels)
    if run_label_key in selector and selector[run_label_key] != run_label:
        raise ValueError(f"Recipe {recipe_id!r} Deployment uses a conflicting Forge run label")
    selector[run_label_key] = run_label
    pod_labels[run_label_key] = run_label
    labels = metadata.setdefault("labels", {})
    if not isinstance(labels, dict):
        raise ValueError(f"Recipe {recipe_id!r} Deployment metadata.labels must be a mapping")
    labels[run_label_key] = run_label
    deployment["metadata"] = {**metadata, "name": deployment_name, "namespace": namespace}

    _strip_hardcoded_uid(pod_spec)

    if service_data is None:
        port = _deployment_service_port(recipe, deployment)
        service_name = _unique_name(f"{original_name}-service")
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": namespace,
                "labels": {run_label_key: run_label},
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {run_label_key: run_label},
                "ports": [{"name": "http", "port": port, "targetPort": port}],
            },
        }
    else:
        service = copy.deepcopy(service_data)
        service_metadata = service["metadata"]
        original_service_name = service_metadata["name"]
        if slugify_identifier(original_service_name) != original_service_name:
            raise ValueError(f"Recipe has an invalid Service name: {original_service_name!r}")
        service_name = _unique_name(original_service_name)
        service_spec = service.get("spec")
        service_selector = service_spec.get("selector") if isinstance(service_spec, dict) else None
        if not isinstance(service_selector, dict) or not service_selector:
            raise ValueError(f"Recipe {recipe_id!r} Service must define spec.selector")
        if any(original_pod_labels.get(key) != value for key, value in service_selector.items()):
            raise ValueError(
                f"Recipe {recipe_id!r} Service selector does not match Deployment pod labels"
            )
        if run_label_key in service_selector and service_selector[run_label_key] != run_label:
            raise ValueError(f"Recipe {recipe_id!r} Service uses a conflicting Forge run label")
        service_spec["selector"] = {**service_selector, run_label_key: run_label}
        port = _service_manifest_port(service)
        service_labels = service_metadata.get("labels") or {}
        if not isinstance(service_labels, dict):
            raise ValueError(f"Recipe {recipe_id!r} Service metadata.labels must be a mapping")
        service["metadata"] = {
            **service_metadata,
            "name": service_name,
            "namespace": namespace,
            "labels": {**service_labels, run_label_key: run_label},
        }

    readiness_timeout = config.project.get_config("inference_playbooks.readiness_timeout_seconds")
    if type(readiness_timeout) is not int or readiness_timeout <= 0:
        raise ValueError("inference_playbooks.readiness_timeout_seconds must be a positive integer")

    service_path = _write_launch_manifest(f"{service_name}-service", service)
    deployment_path = _write_launch_manifest(f"{deployment_name}-deployment", deployment)

    def deploy() -> str:
        oc("apply", "-f", str(service_path))
        oc("apply", "-f", str(deployment_path))
        result = oc(
            "rollout",
            "status",
            f"deployment/{deployment_name}",
            "-n",
            namespace,
            f"--timeout={readiness_timeout}s",
            timeout_seconds=readiness_timeout + 15,
            check=False,
            log_stdout=False,
            log_stderr=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Deployment {deployment_name} did not become ready within "
                f"{readiness_timeout}s: {(result.stderr or '').strip()}"
            )
        return f"http://{service_name}.{namespace}.svc.cluster.local:{port}"

    def capture() -> None:
        for kind, name in (("deployment", deployment_name), ("service", service_name)):
            oc("get", kind, name, "-n", namespace, "-o", "yaml", check=False)
        oc(
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"{run_label_key}={run_label}",
            "-o",
            "yaml",
            check=False,
        )

    def cleanup() -> None:
        _delete_resources(namespace, [("deployment", deployment_name), ("service", service_name)])

    _deploy_benchmark_finalize(
        recipe_id, recipe, namespace, deployment_name, deploy, capture, cleanup
    )


def _service_manifest_port(service: dict) -> int:
    spec = service.get("spec")
    ports = spec.get("ports") if isinstance(spec, dict) else None
    if not isinstance(ports, list) or not ports:
        raise ValueError("Service manifest must define spec.ports")
    http_ports = [
        item for item in ports if isinstance(item, dict) and item.get("name") in {"http", "https"}
    ]
    candidates = http_ports or ports
    if len(candidates) != 1:
        raise ValueError("Service manifest must define one HTTP port for the GuideLLM endpoint")
    port = candidates[0].get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Service manifest must define a valid spec.ports[].port")
    return port


def _deployment_service_port(recipe: dict, deployment: dict) -> int:
    serving = recipe.get("serving")
    port = serving.get("port") if isinstance(serving, dict) else None
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Recipe serving.port must be between 1 and 65535")
        return port

    containers = deployment["spec"]["template"]["spec"]["containers"]
    ports = [
        port
        for container in containers
        if isinstance(container, dict)
        for port in container.get("ports", [])
        if isinstance(port, dict) and type(port.get("containerPort")) is int
    ]
    http_ports = {port["containerPort"] for port in ports if port.get("name") in {"http", "https"}}
    container_ports = {port["containerPort"] for port in ports}
    if len(http_ports) == 1:
        return next(iter(http_ports))
    if len(container_ports) == 1:
        return next(iter(container_ports))
    if not container_ports:
        raise ValueError("Deployment must define serving.port or a containerPort")
    raise ValueError("Deployment has multiple ports; define serving.port for the GuideLLM endpoint")


def _launch_isvc_recipe(
    recipe_id: str,
    recipe: dict,
    manifest_data: dict,
    servingruntime_data: dict | None,
    service_data: dict | None,
    namespace: str,
) -> None:
    if service_data is not None:
        raise ValueError(
            "InferenceService overrides use the KServe predictor endpoint; omit Service"
        )
    from projects.rhaiis.toolbox.capture_isvc_state.main import run as capture_isvc_state
    from projects.rhaiis.toolbox.cleanup_isvc.main import run as cleanup_isvc
    from projects.rhaiis.toolbox.deploy_kserve_isvc.main import run as deploy_kserve_isvc
    from projects.rhaiis.toolbox.wait_isvc_ready.main import run as wait_isvc_ready

    manifest = copy.deepcopy(manifest_data)
    metadata = manifest["metadata"]
    original_name = metadata["name"]
    if slugify_identifier(original_name) != original_name:
        raise ValueError(f"Recipe has an invalid InferenceService name: {original_name!r}")
    deployment_name = _unique_name(original_name)
    manifest["metadata"] = {**metadata, "name": deployment_name, "namespace": namespace}

    servingruntime = copy.deepcopy(servingruntime_data) if servingruntime_data else None
    if servingruntime is not None:
        if servingruntime.get("kind") != "ServingRuntime":
            raise ValueError("InferenceService companion must be a namespaced ServingRuntime")
        runtime_metadata = servingruntime.get("metadata")
        if not isinstance(runtime_metadata, dict) or not isinstance(
            runtime_metadata.get("name"), str
        ):
            raise ValueError("ServingRuntime companion must define metadata.name")
        original_runtime_name = runtime_metadata["name"]
        predictor = manifest.get("spec", {}).get("predictor")
        model_spec = predictor.get("model") if isinstance(predictor, dict) else None
        if isinstance(model_spec, dict) and model_spec.get("runtime") == original_runtime_name:
            model_spec["runtime"] = deployment_name
        servingruntime["metadata"] = {
            **runtime_metadata,
            "name": deployment_name,
            "namespace": namespace,
        }

    _prepare_isvc_model(recipe_id, recipe, manifest, namespace)
    manifest_path = _write_launch_manifest(f"{deployment_name}-inferenceservice", manifest)
    servingruntime_path = (
        _write_launch_manifest(f"{deployment_name}-servingruntime", servingruntime)
        if servingruntime is not None
        else None
    )

    readiness_timeout = config.project.get_config("inference_playbooks.readiness_timeout_seconds")
    if type(readiness_timeout) is not int or readiness_timeout <= 0:
        raise ValueError("inference_playbooks.readiness_timeout_seconds must be a positive integer")

    def deploy() -> str:
        if servingruntime_path is not None:
            deploy_kserve_isvc(
                namespace=namespace,
                servingruntime_file=str(servingruntime_path),
                inferenceservice_file=str(manifest_path),
            )
        else:
            oc("create", "-f", str(manifest_path))
        wait_isvc_ready(
            name=deployment_name,
            namespace=namespace,
            timeout_seconds=readiness_timeout,
        )
        inferenceservice = oc_get_json(
            "inferenceservice", name=deployment_name, namespace=namespace
        )
        status = inferenceservice.get("status")
        endpoint_url = status.get("url") if isinstance(status, dict) else None
        parsed_url = urlparse(endpoint_url) if isinstance(endpoint_url, str) else None
        if parsed_url and parsed_url.scheme in {"http", "https"} and parsed_url.hostname:
            return endpoint_url
        port = _isvc_service_port(recipe, manifest)
        return f"http://{deployment_name}-predictor.{namespace}.svc.cluster.local:{port}"

    def capture() -> None:
        capture_isvc_state(name=deployment_name, namespace=namespace)

    def cleanup() -> None:
        cleanup_isvc(name=deployment_name, namespace=namespace)

    _deploy_benchmark_finalize(
        recipe_id, recipe, namespace, deployment_name, deploy, capture, cleanup
    )


def _prepare_isvc_model(
    recipe_id: str,
    recipe: dict,
    manifest: dict,
    namespace: str,
) -> None:
    spec = manifest.get("spec")
    predictor = spec.get("predictor") if isinstance(spec, dict) else None
    model = predictor.get("model") if isinstance(predictor, dict) else None
    if not isinstance(model, dict):
        return
    storage_uri = model.get("storageUri")
    if not isinstance(storage_uri, str) or not storage_uri.startswith("hf://"):
        return

    cache_spec = _prepare_hf_model_cache(
        recipe_id,
        storage_uri,
        namespace,
        model_cache=recipe.get("model_cache"),
    )
    model["storageUri"] = cache_spec["model_uri"]
    metadata = manifest.setdefault("metadata", {})
    annotations = metadata.setdefault("annotations", {})
    if not isinstance(annotations, dict):
        raise ValueError("InferenceService metadata.annotations must be a mapping")
    annotations["serving.kserve.io/deploymentMode"] = "RawDeployment"


def _isvc_service_port(recipe: dict, manifest: dict) -> int:
    serving = recipe.get("serving")
    port = serving.get("port") if isinstance(serving, dict) else None
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Recipe serving.port must be between 1 and 65535")
        return port

    spec = manifest.get("spec")
    predictor = spec.get("predictor") if isinstance(spec, dict) else None
    containers = predictor.get("containers", []) if isinstance(predictor, dict) else []
    ports = {
        port["containerPort"]
        for container in containers
        if isinstance(container, dict)
        for port in container.get("ports", [])
        if isinstance(port, dict) and type(port.get("containerPort")) is int
    }
    if len(ports) == 1:
        return next(iter(ports))
    if not ports:
        raise ValueError(
            "InferenceService has no status.url and declares no serving.port or containerPort"
        )
    raise ValueError("InferenceService has multiple ports; define serving.port")


def _deploy_benchmark_finalize(
    recipe_id: str,
    recipe: dict,
    namespace: str,
    run_name: str,
    deploy,
    capture,
    cleanup,
) -> None:
    workload_configs = _get_workload_configs()
    primary_exc = None
    finalizer_exc = None
    endpoint_url = None
    try:
        logger.info("Launching recipe %s as %s in namespace %s", recipe_id, run_name, namespace)
        endpoint_url = deploy()
        logger.info("Recipe %s reached Ready at %s", recipe_id, endpoint_url)
        for workload_key, workload in workload_configs:
            kpi_labels = _benchmark_kpi_labels(recipe_id, recipe, workload_key, workload)
            logger.info("Running workload %s (%s)", workload_key, workload.get("tool", "guidellm"))
            _run_workload(
                endpoint_url,
                namespace,
                run_name,
                recipe.get("served_model_name", recipe["model_name"]),
                workload_key,
                workload,
                kpi_labels,
            )
    except Exception:
        primary_exc = sys.exc_info()
    finally:
        for description, callback in (
            ("capturing recipe state", capture),
            ("cleaning up", cleanup),
        ):
            try:
                callback()
            except Exception:
                if primary_exc is None:
                    logger.exception("Finalizer failed while %s", description)
                    finalizer_exc = finalizer_exc or sys.exc_info()
                else:
                    logger.exception(
                        "Ignoring %s failure after the primary test failure", description
                    )

    if primary_exc is not None:
        raise primary_exc[1].with_traceback(primary_exc[2])
    if finalizer_exc is not None:
        raise finalizer_exc[1].with_traceback(finalizer_exc[2])


def _benchmark_kpi_labels(
    recipe_id: str, recipe: dict, workload_key: str, workload: dict | None = None
) -> dict[str, str]:
    platform = recipe.get("platform")
    product_version = platform.get("version") if isinstance(platform, dict) else None
    if not product_version:
        for path_key in ("recipe_path", "manifest_path"):
            path = recipe.get(path_key)
            if not path:
                continue
            parts = Path(path).parts
            for stack, prefix in (("rhoai", "RHOAI-"), ("vllm", "")):
                if stack not in parts:
                    continue
                version_index = parts.index(stack) + 1
                if version_index < len(parts) and parts[version_index] != "latest":
                    product_version = f"{prefix}{parts[version_index]}"
                    break
            if product_version:
                break
    if not product_version:
        product_version = "unknown"
        logger.warning("Recipe %s has no product version metadata", recipe_id)

    hardware_profile = recipe.get("hardware_profile_data", {})
    if not isinstance(hardware_profile, dict):
        hardware_profile = {}
    accelerators = hardware_profile.get("accelerators", {})
    if not isinstance(accelerators, dict):
        accelerators = {}
    gpu_type = accelerators.get("model")
    vendor = accelerators.get("vendor")
    if not gpu_type:
        hardware = config.project.get_config("ci_job.hardware")
        if isinstance(hardware, dict):
            gpu_type = hardware.get("gpuType") or hardware.get("gpu_type")
    if not gpu_type:
        gpu_type = "unknown"
        logger.warning("Recipe %s has no GPU model metadata", recipe_id)
    gpu_type = str(gpu_type)
    if vendor:
        vendor = str(vendor).upper()
        if not gpu_type.upper().startswith(f"{vendor}-"):
            gpu_type = f"{vendor}-{gpu_type}"

    return {
        "model_name": str(recipe["model_name"]),
        "product_version": str(product_version),
        "deployment_profile": str(recipe.get("deployment_mode") or recipe_id),
        "guidellm_loadshape": workload_key,
        "gpu_type": str(gpu_type),
        "platform": KPI_PLATFORM,
        "test_harness": KPI_TEST_HARNESS,
        "benchmark_key": workload_key,
        "benchmark_tool": workload.get("tool", "guidellm")
        if isinstance(workload, dict)
        else "guidellm",
    }


def _get_workload_configs() -> list[tuple[str, dict]]:
    """Resolve one or more workload keys into validated (key, config) pairs.

    Accepts a single string (``"profile1"``), a comma-separated string
    (``"profile1,aiperf-agentx-128k"``), or a YAML list.
    """
    raw = config.project.get_config("inference_playbooks.workload")
    if isinstance(raw, str):
        keys = [k.strip() for k in raw.split(",") if k.strip()]
    elif isinstance(raw, list):
        keys = [str(k).strip() for k in raw if str(k).strip()]
    else:
        raise ValueError("inference_playbooks.workload must be a string or list")
    if not keys:
        raise ValueError("inference_playbooks.workload must specify at least one workload key")

    workloads = config.Config(RHAIIS_WORKLOADS).config
    results = []
    for key in keys:
        if key not in workloads:
            available = ", ".join(sorted(workloads))
            raise ValueError(f"Unknown workload {key!r}; available: {available}")
        workload = workloads[key]
        if not isinstance(workload, dict):
            raise ValueError(f"Workload {key!r} must be a mapping")
        tool = workload.get("tool", "guidellm")
        if tool == "guidellm":
            missing = {"data", "rates", "max_seconds"} - workload.keys()
            if missing:
                raise ValueError(
                    f"GuideLLM workload {key!r} is missing required fields: {sorted(missing)}"
                )
        results.append((key, workload))
    return results


def _run_workload(
    endpoint_url: str,
    namespace: str,
    run_name: str,
    model_name: str,
    workload_key: str,
    workload: dict,
    kpi_labels: dict[str, str],
) -> None:
    tool = workload.get("tool", "guidellm")
    if tool == "guidellm":
        _run_guidellm_workload(
            endpoint_url, namespace, run_name, model_name, workload_key, workload, kpi_labels
        )
    elif tool == "aiperf":
        _run_aiperf_workload(
            endpoint_url, namespace, run_name, model_name, workload_key, workload, kpi_labels
        )
    else:
        raise ValueError(f"Unsupported benchmark tool {tool!r} in workload {workload_key!r}")


def _run_guidellm_workload(
    endpoint_url: str,
    namespace: str,
    run_name: str,
    model_name: str,
    workload_key: str,
    workload: dict,
    kpi_labels: dict[str, str],
) -> None:
    benchmark = config.Config(RHAIIS_CONFIG).get_config("benchmarks.guidellm")

    def run_phase(phase: str, rates: list[int], max_seconds: int, rampup: int | None) -> None:
        args = {
            **benchmark["args"],
            "data": workload["data"],
            "max_seconds": max_seconds,
            "processor": model_name,
        }
        if rampup is not None:
            args["rampup"] = rampup
        with env.NextArtifactDir(f"{phase}_{workload_key}"):
            labels = {"phase": phase}
            if phase == "warmup":
                labels["skip"] = True
            create_test_metadata(
                env.ARTIFACT_DIR,
                labels,
                kpi_labels=kpi_labels,
            )
            run_guidellm_benchmark.run(
                endpoint_url=endpoint_url,
                name=_unique_name(f"guidellm-{phase}-{run_name}"),
                namespace=namespace,
                image=benchmark["image"],
                timeout=benchmark["timeout"],
                pvc_size=benchmark["pvc_size"],
                guidellm_args=build_guidellm_args({"args": args, "rate": rates}),
                hf_token_secret="",
                fs_group=benchmark.get("fs_group"),
            )

    if workload.get("warmup") is not None:
        run_phase("warmup", [1], workload["warmup"], None)
    run_phase("benchmark", workload["rates"], workload["max_seconds"], workload.get("rampup"))


def _run_aiperf_workload(
    endpoint_url: str,
    namespace: str,
    run_name: str,
    model_name: str,
    workload_key: str,
    workload: dict,
    kpi_labels: dict[str, str],
) -> None:
    from projects.aiperf.toolbox.run_aiperf_benchmark import main as run_aiperf_benchmark

    aiperf_cfg = config.Config(RHAIIS_CONFIG).get_config("benchmarks.aiperf")
    concurrency_values = workload.get("concurrency")
    if isinstance(concurrency_values, list):
        sweep = concurrency_values
    elif concurrency_values is not None:
        sweep = [concurrency_values]
    else:
        sweep = [None]

    for concurrency in sweep:
        label = f"concurrency-{concurrency}" if concurrency is not None else "default"
        phase_name = f"benchmark_{workload_key}"
        if len(sweep) > 1:
            phase_name = f"{phase_name}_{label}"
        with env.NextArtifactDir(phase_name):
            run_labels = {
                "phase": "benchmark",
                "benchmark_tool": "aiperf",
            }
            run_kpi_labels = {**kpi_labels, "benchmark_tool": "aiperf"}
            if concurrency is not None:
                run_labels["concurrency"] = concurrency
                run_kpi_labels["concurrency"] = str(concurrency)
            create_test_metadata(
                env.ARTIFACT_DIR,
                run_labels,
                kpi_labels=run_kpi_labels,
            )
            job_name = _unique_name(f"aiperf-{workload_key}-{run_name}")
            if len(sweep) > 1:
                job_name = _unique_name(f"aiperf-c{concurrency}-{run_name}")

            run_aiperf_benchmark.run(
                endpoint_url=endpoint_url,
                model_name=model_name,
                namespace=namespace,
                name=job_name,
                image=aiperf_cfg["image"],
                timeout=workload.get("timeout_seconds", aiperf_cfg["timeout"]),
                pvc_size=aiperf_cfg["pvc_size"],
                pvc_storage_class=aiperf_cfg.get("pvc_storage_class"),
                tokenizer=workload.get("tokenizer", model_name),
                endpoint_type=workload.get("endpoint_type", "chat"),
                endpoint_path=workload.get("endpoint_path", "/v1/chat/completions"),
                streaming=workload.get("streaming", True),
                concurrency=concurrency,
                request_rate=workload.get("request_rate"),
                request_count=workload.get("request_count"),
                input_tokens=workload.get("input_tokens"),
                output_tokens=workload.get("output_tokens"),
                ssl_verify=workload.get("ssl_verify", True),
                aiperf_args=workload.get("aiperf_args"),
            )


def _delete_resources(namespace: str, resources: list[tuple[str, str]]) -> None:
    failures = []
    for kind, name in resources:
        result = oc(
            "delete",
            kind,
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "--timeout=120s",
            check=False,
            timeout_seconds=180,
        )
        if result.returncode:
            failures.append(f"{kind}/{name}: {result.stderr.strip()}")
        else:
            logger.info("Removed test %s/%s", kind, name)
    if failures:
        raise RuntimeError("Failed to clean up: " + "; ".join(failures))


def _write_launch_manifest(name: str, manifest: dict) -> Path:
    path = env.ARTIFACT_DIR / "src" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def _unique_name(value: str) -> str:
    return f"{slugify_identifier(value, max_length=46)}-{uuid.uuid4().hex[:8]}"


def fournos_resolve_hardware_request(hardware_spec: dict):
    """Preserve explicit /gpu requests from the PR comment."""
    return hardware_spec
