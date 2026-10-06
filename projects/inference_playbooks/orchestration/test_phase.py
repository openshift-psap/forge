import copy
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import yaml

from projects.core.dsl.utils import slugify_identifier
from projects.core.dsl.utils.k8s import oc
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
    workload_key = config.project.get_config("inference_playbooks.workload") if recipe_id else None
    if recipe_id and (not isinstance(workload_key, str) or not workload_key):
        raise ValueError("inference_playbooks.workload must be a configured workload key")
    labels = {
        "project": "inference-playbooks",
        "validation": f"recipe-{workload_key}" if recipe_id else "recipe-catalog-validation",
    }
    if recipe_id:
        labels["recipe_id"] = recipe_id
        labels["workload_key"] = workload_key
        # The parent contains both warmup and measured artifacts; parse phase-level nodes only.
        labels["skip"] = True
    test_dir = env.ARTIFACT_DIR
    create_test_metadata(test_dir, labels)
    return test_dir


def do_test():
    logger.info("=== Inference Playbooks Project Test Phase ===")
    repository_path = foreign_repository.initialize()
    recipes = load_recipe_catalog(repository_path)
    logger.info("Validated %d Inference Playbooks recipe(s)", len(recipes))

    recipe_id = config.project.get_config("inference_playbooks.recipe")
    if recipe_id is not None and not isinstance(recipe_id, str):
        raise ValueError("inference_playbooks.recipe must be a recipe ID string")

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
                _launch_recipe(recipe_id, recipes[recipe_id])
        except Exception as exc:
            logger.exception("Test failed with exception")
            update_test_labels_with_status(test_dir, False, f"Test failed: {exc}")
            raise
        finally:
            update_test_labels_with_timing(test_dir, "test", "end")

    update_test_labels_with_status(test_dir, True, "Test completed successfully")
    return 0


def _clusterless_mode() -> bool:
    return os.environ.get("CLUSTERLESS_MODE", "").lower() == "true" or not os.environ.get(
        "KUBECONFIG"
    )


def _launch_recipe(recipe_id: str, recipe: dict) -> None:
    if _clusterless_mode():
        raise RuntimeError(
            "A recipe launch needs a target cluster; rerun the PR test with /cluster <registered-target>"
        )

    manifest = recipe["manifest_data"]
    namespace = config.project.get_config("inference_playbooks.namespace") or manifest.get(
        "metadata", {}
    ).get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError(
            "Recipe launch needs a namespace; set /var inference_playbooks.namespace: <namespace>"
        )
    if slugify_identifier(namespace) != namespace:
        raise ValueError(f"Invalid Kubernetes namespace: {namespace!r}")

    recipe_type = recipe.get("recipe_type")
    if recipe_type == "llmisvc":
        _launch_llmisvc_recipe(recipe_id, recipe, namespace)
    elif recipe_type == "recipe-v3":
        _launch_lws_recipe(recipe_id, recipe, namespace)
    else:
        raise ValueError(f"Recipe {recipe_id!r} has unsupported type {recipe_type!r}")


def _launch_llmisvc_recipe(recipe_id: str, recipe: dict, namespace: str) -> None:
    manifest = copy.deepcopy(recipe["manifest_data"])
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
    model_uri = manifest["spec"]["model"]["uri"]
    if model_uri.startswith("hf://"):
        cache_spec = _prepare_hf_model_cache(recipe_id, model_uri, namespace)
        manifest["spec"]["model"]["uri"] = cache_spec["model_uri"]
        manifest["spec"]["storageInitializer"] = {"enabled": False}
        logger.info("Prepared model cache %s for %s", cache_spec["pvc_name"], model_uri)
        return

    model_source = recipe.get("model_source", {})
    pvc_name = model_uri.removeprefix("pvc://").split("/", maxsplit=1)[0]
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


def _launch_lws_recipe(recipe_id: str, recipe: dict, namespace: str) -> None:
    lws = copy.deepcopy(recipe["manifest_data"])
    services = [
        copy.deepcopy(source["data"])
        for source in recipe.get("auxiliary_manifests", [])
        if source["data"].get("kind") == "Service"
    ]
    if len(services) != 1:
        raise ValueError(f"Recipe {recipe_id!r} must define exactly one auxiliary Service")

    original_name = lws["metadata"]["name"]
    if slugify_identifier(original_name) != original_name:
        raise ValueError(f"Recipe has an invalid LeaderWorkerSet name: {original_name!r}")
    original_service_name = services[0]["metadata"]["name"]
    lws_name = _unique_name(original_name)
    service_name = _unique_name(original_service_name)
    run_label = lws_name
    endpoint_label = "leader"
    lws["metadata"] = {
        **lws["metadata"],
        "name": lws_name,
        "namespace": namespace,
        "labels": {**lws["metadata"].get("labels", {}), "forge.openshift.io/run": run_label},
    }
    templates = lws["spec"]["leaderWorkerTemplate"]
    leader_template = templates.get("leaderTemplate")
    if not isinstance(leader_template, dict):
        raise ValueError(f"Recipe {recipe_id!r} must define an explicit leaderTemplate")
    leader_labels = leader_template.setdefault("metadata", {}).setdefault("labels", {})
    leader_labels.update(
        {"forge.openshift.io/run": run_label, "forge.openshift.io/endpoint": endpoint_label}
    )
    worker_labels = templates["workerTemplate"].setdefault("metadata", {}).setdefault("labels", {})
    worker_labels["forge.openshift.io/run"] = run_label

    rdma_resource = config.project.get_config("inference_playbooks.rdma_resource")
    if rdma_resource:
        deploy_lws.replace_resource_name(lws, "rdma/ib", rdma_resource)
    lws_ready_timeout_seconds = config.project.get_config(
        "inference_playbooks.lws_ready_timeout_seconds"
    )

    service = services[0]
    service["metadata"] = {
        **service["metadata"],
        "name": service_name,
        "namespace": namespace,
        "labels": {
            **service["metadata"].get("labels", {}),
            "forge.openshift.io/run": run_label,
        },
    }
    service["spec"]["selector"] = {
        "forge.openshift.io/run": run_label,
        "forge.openshift.io/endpoint": endpoint_label,
    }
    pod_selector = f"forge.openshift.io/run={run_label}"

    def deploy() -> str:
        _prepare_lws_model_cache(recipe, lws, namespace)
        service_manifest_path = _write_launch_manifest(f"{service_name}-service", service)
        lws_manifest_path = _write_launch_manifest(f"{lws_name}-lws", lws)
        return deploy_lws.run(
            namespace=namespace,
            service_manifest_path=str(service_manifest_path),
            leader_worker_set_manifest_path=str(lws_manifest_path),
            pod_selector=pod_selector,
            timeout_seconds=lws_ready_timeout_seconds,
        )

    def capture() -> None:
        capture_lws_state.run(
            namespace=namespace,
            lws_name=lws_name,
            pod_selector=pod_selector,
        )

    def cleanup() -> None:
        _delete_resources(
            namespace,
            [("leaderworkerset", lws_name), ("service", service_name)],
        )

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


def _deploy_benchmark_finalize(
    recipe_id: str,
    recipe: dict,
    namespace: str,
    run_name: str,
    deploy,
    capture,
    cleanup,
) -> None:
    workload_key, workload = _get_workload_config()
    primary_exc = None
    finalizer_exc = None
    endpoint_url = None
    try:
        logger.info("Launching recipe %s as %s in namespace %s", recipe_id, run_name, namespace)
        endpoint_url = deploy()
        logger.info("Recipe %s reached Ready at %s", recipe_id, endpoint_url)
        _run_workload(
            endpoint_url, namespace, run_name, recipe["model_name"], workload_key, workload
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


def _get_workload_config() -> tuple[str, dict]:
    workload_key = config.project.get_config("inference_playbooks.workload")
    workloads = config.Config(RHAIIS_WORKLOADS).config
    if not isinstance(workload_key, str) or workload_key not in workloads:
        available = ", ".join(sorted(workloads))
        raise ValueError(
            f"Unknown inference playbooks workload {workload_key!r}; available: {available}"
        )
    workload = workloads[workload_key]
    if not isinstance(workload, dict):
        raise ValueError(f"Workload {workload_key!r} must be a mapping")
    missing = {"data", "rates", "max_seconds"} - workload.keys()
    if missing:
        raise ValueError(f"Workload {workload_key!r} is missing required fields: {sorted(missing)}")
    return workload_key, workload


def _run_workload(
    endpoint_url: str,
    namespace: str,
    run_name: str,
    model_name: str,
    workload_key: str,
    workload: dict,
) -> None:
    benchmark = config.Config(RHAIIS_CONFIG).get_config("benchmarks.guidellm")

    def run_phase(phase: str, rates: list[int], max_seconds: int, rampup: int | None) -> None:
        args = {
            **benchmark["args"],
            "data": workload["data"],
            "max_seconds": max_seconds,
            # GuideLLM discovers the served model from /v1/models; processor
            # selects the matching tokenizer for generated workload prompts.
            "processor": model_name,
        }
        if rampup is not None:
            args["rampup"] = rampup
        with env.NextArtifactDir(f"{phase}_{workload_key}"):
            create_test_metadata(
                env.ARTIFACT_DIR,
                {"phase": phase, "skip": phase == "warmup"},
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
    return f"{slugify_identifier(value, max_length=54)}-{uuid.uuid4().hex[:8]}"


def fournos_resolve_hardware_request(hardware_spec: dict):
    """Preserve explicit /gpu requests from the PR comment."""
    return hardware_spec
