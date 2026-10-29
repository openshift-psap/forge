import json
import logging
import os
import re
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path

import yaml

from projects.core.dsl.utils import slugify_identifier
from projects.core.dsl.utils.k8s import oc, oc_get_json
from projects.core.library import config, env
from projects.core.library.postprocess import (
    create_test_metadata,
    run_and_postprocess,
    update_test_labels_with_status,
    update_test_labels_with_timing,
)
from projects.foreign_testing.library import initialize as foreign_repository
from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark import main as run_guidellm_benchmark
from projects.inference_playbooks.orchestration.manifest_bundle import (
    RESOURCE_TYPES,
    checked_submission,
    classify_documents,
    load_metadata_document,
    load_source_bundle,
    normalized_documents,
    write_bundle_artifacts,
)
from projects.inference_playbooks.orchestration.recipe_validation import (
    load_recipe_catalog,
    resolve_model_quantization,
)
from projects.inference_playbooks.toolbox.wait_recipe import main as wait_recipe

logger = logging.getLogger(__name__)
PROJECTS_DIR = Path(__file__).resolve().parents[2]
RHAIIS_CONFIG = PROJECTS_DIR / "rhaiis/orchestration/config.yaml"
RHAIIS_WORKLOADS = PROJECTS_DIR / "rhaiis/orchestration/config.d/workloads.yaml"
KPI_PLATFORM = "OCP"
KPI_TEST_HARNESS = "forge-inference-playbooks"


def test():
    """Run the Inference Playbooks test with outcome postprocessing."""
    recipe_selector = config.project.get_config("inference_playbooks.recipe")
    postprocess_enabled = (
        bool(config.project.get_config("caliper.postprocess.enabled"))
        and bool(recipe_selector)
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
        if config.project.get_config("inference_playbooks.deploy.node_selector"):
            labels["submission_mode"] = "ci-node-selector-override"
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
    recipe_selector = config.project.get_config("inference_playbooks.recipe")
    manifest_path = config.project.get_config("inference_playbooks.manifest_path")
    if recipe_selector is not None or manifest_path is not None:
        _require_manifest_path(manifest_path)
        if recipe_selector is None:
            raise ValueError(
                "A <model>/recipes/<recipe-folder> selector is required when inference_playbooks.manifest_path is set"
            )
    if recipe_selector is not None:
        recipe = _load_recipe_metadata(repository_path, recipe_selector)
        recipe_id = recipe["recipe_id"]
        recipe_count = 1
    else:
        recipe_count = len(load_recipe_catalog(repository_path))
        recipe = None
        recipe_id = None

    logger.info("Loaded metadata for %d Inference Playbooks recipe(s)", recipe_count)

    with env.NextArtifactDir("inference_playbooks_test_dir"):
        test_dir = create_custom_test_metadata(recipe_id)
        try:
            update_test_labels_with_timing(test_dir, "test", "start")
            if recipe is None:
                logger.info("No recipe selected; validated recipes without launching a service")
            else:
                _launch_recipe(recipe_id, recipe)
        except Exception as exc:
            logger.exception("Test failed with exception")
            update_test_labels_with_status(test_dir, False, f"Test failed: {exc}")
            raise
        finally:
            update_test_labels_with_timing(test_dir, "test", "end")

    update_test_labels_with_status(test_dir, True, "Test completed successfully")
    return 0


def _load_recipe_metadata(repository_path: Path, selector: str) -> dict:
    root = repository_path.resolve(strict=True)
    if not isinstance(selector, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]*/recipes/[a-z0-9][a-z0-9._-]*", selector
    ):
        raise ValueError(
            "inference_playbooks.recipe must be a canonical <model>/recipes/<recipe-folder> selector"
        )
    recipe_path = _repository_file(root, f"models/{selector}/recipe.yaml", "selected recipe")
    recipe_data = _load_yaml_document(recipe_path)
    if not isinstance(recipe_data.get("recipe_id"), str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]*", recipe_data["recipe_id"]
    ):
        raise ValueError("Selected recipe must define its authoritative recipe_id")
    model_id = recipe_data.get("model_id")
    if not isinstance(model_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", model_id):
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
        "repository_path": root,
        "model_name": model_name,
        "served_model_name": served_model_name,
        "quantization": resolve_model_quantization(recipe_data, model_data),
        "model_cache": model_cache,
        "hardware_profile_data": hardware_profile_data,
        "platform": platform,
    }


def _repository_file(
    root: Path,
    value: str,
    label: str,
) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty path")
    relative_path = Path(value)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.as_posix() != value
    ):
        raise ValueError(f"{label} must be a canonical checkout-relative path")
    candidate = root / relative_path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"{label} does not exist in the Inference Playbooks checkout") from None
    if candidate != resolved or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"{label} must be a canonical contained file, without symlinks")
    return resolved


def _load_yaml_document(path: Path):
    return load_metadata_document(path.read_bytes())


def _load_manifest_override(
    repository_path: Path,
    manifest_path: str,
    recipe: dict,
) -> dict:
    root = repository_path.resolve(strict=True)
    _require_manifest_path(manifest_path)
    relative = Path(manifest_path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != manifest_path
        or len(relative.parts) != 3
        or relative.parts[0] != "manifests"
    ):
        raise ValueError(
            "inference_playbooks.manifest_path must be canonical recipe-relative manifests/<platform>/<file>.yaml"
        )
    recipe_path = Path(recipe["recipe_path"])
    if not recipe_path.is_relative_to(root) or recipe_path != recipe_path.resolve(strict=True):
        raise ValueError("Selected recipe must be canonical and contained in the checkout")
    selected = recipe_path.parent / relative
    try:
        canonical = selected.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(
            "inference_playbooks.manifest_path does not identify an existing artifact"
        ) from None
    if (
        selected != canonical
        or not selected.is_file()
        or not canonical.is_relative_to(recipe_path.parent)
    ):
        raise ValueError(
            "inference_playbooks.manifest_path must be a canonical contained artifact file"
        )
    return load_source_bundle(root, recipe, selected)


def _require_manifest_path(value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "Every recipe launch requires an explicit nonempty inference_playbooks.manifest_path"
        )


def _classify_manifest_documents(documents: list[dict]) -> dict:
    return classify_documents(documents)


def _manifest_bundle_from_recipe(recipe: dict) -> dict:
    selection = config.project.get_config("inference_playbooks.manifest_path")
    _require_manifest_path(selection)
    if "repository_path" not in recipe or "recipe_path" not in recipe:
        raise ValueError("Strict certification requires a canonical v4 recipe checkout")
    return _load_manifest_override(recipe["repository_path"], selection, recipe)


def _launch_recipe(recipe_id: str, recipe: dict) -> None:
    bundle = _manifest_bundle_from_recipe(recipe)
    if _clusterless_mode():
        raise RuntimeError(
            "A recipe launch needs a target cluster; rerun the PR test with /cluster <registered-target>"
        )

    if bundle["recipe_id"] != recipe_id:
        raise ValueError("Bundle recipe identity does not match the selected recipe")
    recipe["platform"] = bundle["platform"]
    logger.info("Using complete explicitly selected platform %s", bundle["platform"])
    manifest = bundle["primary"]
    namespace = config.project.get_config("inference_playbooks.namespace")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError(
            "Recipe launch needs a namespace; set /var inference_playbooks.namespace: <namespace>"
        )
    if slugify_identifier(namespace) != namespace:
        raise ValueError(f"Invalid Kubernetes namespace: {namespace!r}")

    if config.project.get_config("inference_playbooks.rdma_resource"):
        raise ValueError("RDMA resource overrides are forbidden; update the canonical recipe")
    node_selector = config.project.get_config("inference_playbooks.deploy.node_selector")
    submitted = normalized_documents(bundle["documents"], namespace, node_selector)
    if node_selector:
        logger.info("Applying declared CI node selector to serving pod templates")
    kind = manifest["kind"]
    port = (
        _recipe_http_port(recipe, manifest) if kind in {"Deployment", "LeaderWorkerSet"} else None
    )
    timeout_key = (
        "lws_ready_timeout_seconds" if kind == "LeaderWorkerSet" else "readiness_timeout_seconds"
    )
    timeout = config.project.get_config(f"inference_playbooks.{timeout_key}")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("Recipe readiness timeout must be a positive integer")
    src = env.ARTIFACT_DIR / "src"
    metadata = write_bundle_artifacts(
        bundle, submitted, namespace, src, env.FORGE_HOME, node_selector
    )
    # Keep existing primary launch files as attribution inputs, but never submit from them.
    suffixes = {
        "LeaderWorkerSet": "-lws",
        "Deployment": "-deployment",
        "InferenceService": "-inferenceservice",
        "ServingRuntime": "-servingruntime",
        "LLMInferenceService": "",
    }
    for document in submitted:
        _write_launch_manifest(document["metadata"]["name"] + suffixes[document["kind"]], document)
    created = []
    name = manifest["metadata"]["name"]

    def deploy():
        _create_checked_bundle(src, namespace, metadata, created)
        primary = next(resource for resource in created if resource["kind"] == kind)
        return wait_recipe.run(
            namespace=namespace,
            kind=kind,
            name=name,
            uid=primary["metadata"]["uid"],
            timeout_seconds=timeout,
            port=port,
        )

    def capture():
        _capture_created_recipe(namespace, created)

    def cleanup():
        _delete_created_resources(namespace, created)

    _deploy_benchmark_finalize(recipe_id, recipe, namespace, name, deploy, capture, cleanup)


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
        serving = recipe.get("serving")
        serving_model = serving.get("model") if isinstance(serving, dict) else None
        tokenizer_model = (
            serving_model
            if isinstance(serving_model, str) and not Path(serving_model).is_absolute()
            else recipe["model_name"]
        )
        serving_args = serving.get("args", []) if isinstance(serving, dict) else []
        tokenizer_args = (
            {"trust_remote_code": True}
            if isinstance(serving_args, list)
            and any(
                isinstance(arg, dict)
                and arg.get("flag") == "--trust-remote-code"
                and arg.get("required") is True
                for arg in serving_args
            )
            else {}
        )
        served_model_name = recipe.get("served_model_name", recipe["model_name"])
        for workload_key, workload in workload_configs:
            kpi_labels = _benchmark_kpi_labels(recipe_id, recipe, workload_key, workload)
            logger.info("Running workload %s (%s)", workload_key, workload.get("tool", "guidellm"))
            _run_workload(
                endpoint_url,
                namespace,
                run_name,
                tokenizer_model,
                served_model_name,
                workload_key,
                workload,
                kpi_labels,
                tokenizer_args=tokenizer_args,
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

    labels = {
        "model_name": str(recipe["model_name"]),
        "product_version": str(product_version),
        "deployment_profile": str(recipe.get("deployment_mode") or recipe_id),
        "guidellm_loadshape": workload_key,
        "gpu_type": str(gpu_type),
        "platform": KPI_PLATFORM,
        "test_harness": KPI_TEST_HARNESS,
        "benchmark_key": workload_key,
        "benchmark_tool": (
            workload.get("tool", "guidellm") if isinstance(workload, dict) else "guidellm"
        ),
    }
    serving = recipe.get("serving")
    checkpoint = serving.get("model") if isinstance(serving, dict) else None
    if isinstance(checkpoint, str) and checkpoint:
        labels["served_model"] = checkpoint
    quantization = recipe.get("quantization")
    if isinstance(quantization, str) and quantization:
        labels["quantization"] = quantization
    return labels


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
    tokenizer_model: str,
    served_model_name: str,
    workload_key: str,
    workload: dict,
    kpi_labels: dict[str, str],
    tokenizer_args: dict[str, bool] | None = None,
) -> None:
    tool = workload.get("tool", "guidellm")
    if tool == "guidellm":
        _run_guidellm_workload(
            endpoint_url,
            namespace,
            run_name,
            tokenizer_model,
            workload_key,
            workload,
            kpi_labels,
            tokenizer_args=tokenizer_args,
        )
    elif tool == "aiperf":
        _run_aiperf_workload(
            endpoint_url,
            namespace,
            run_name,
            served_model_name,
            tokenizer_model,
            workload_key,
            workload,
            kpi_labels,
        )
    else:
        raise ValueError(f"Unsupported benchmark tool {tool!r} in workload {workload_key!r}")


def _run_guidellm_workload(
    endpoint_url: str,
    namespace: str,
    run_name: str,
    tokenizer_model: str,
    workload_key: str,
    workload: dict,
    kpi_labels: dict[str, str],
    tokenizer_args: dict[str, bool] | None = None,
) -> None:
    benchmark = config.Config(RHAIIS_CONFIG).get_config("benchmarks.guidellm")
    artifact_workload_key = workload_key.replace("/", "-")

    def run_phase(phase: str, rates: list[int], max_seconds: int, rampup: int | None) -> None:
        args = {
            **benchmark["args"],
            "data": workload["data"],
            "max_seconds": max_seconds,
            "processor": tokenizer_model,
        }
        if tokenizer_args:
            args["processor_args"] = json.dumps(tokenizer_args)
        if rampup is not None:
            args["rampup"] = rampup
        with env.NextArtifactDir(f"{phase}_{artifact_workload_key}"):
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
    served_model_name: str,
    tokenizer_model: str,
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

    sweep_id = uuid.uuid4().hex if concurrency_values is not None else None
    for concurrency in sweep:
        label = f"concurrency-{concurrency}" if concurrency is not None else "default"
        phase_name = f"benchmark_{workload_key}"
        if len(sweep) > 1:
            phase_name = f"{phase_name}_{label}"
        with env.NextArtifactDir(phase_name):
            run_labels = {
                "phase": "benchmark",
                "benchmark_tool": "aiperf",
                "benchmark_key": workload_key,
            }
            if sweep_id is not None:
                run_labels["sweep_id"] = sweep_id
            run_kpi_labels = {**kpi_labels, **run_labels}
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
                model_name=served_model_name,
                namespace=namespace,
                name=job_name,
                image=aiperf_cfg["image"],
                timeout=workload.get("timeout_seconds", aiperf_cfg["timeout"]),
                pvc_size=aiperf_cfg["pvc_size"],
                pvc_storage_class=aiperf_cfg.get("pvc_storage_class"),
                tokenizer=workload.get("tokenizer", tokenizer_model),
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


def _resource_url(document: dict, namespace: str) -> str:
    version, plural = RESOURCE_TYPES[document["kind"]]
    return f"/apis/{version}/namespaces/{namespace}/{plural}/{document['metadata']['name']}"


def _resource_type(document: dict) -> str:
    version, plural = RESOURCE_TYPES[document["kind"]]
    return f"{plural}.{version.split('/')[0]}"


def _create_checked_bundle(src: Path, namespace: str, metadata: dict, created: list[dict]) -> None:
    documents = checked_submission(src, namespace, metadata)
    # Provisioning and isolation are external prerequisites, not serving repairs.
    oc_get_json("namespaces", name=namespace)
    for document in documents:
        existing = oc_get_json(
            _resource_type(document),
            name=document["metadata"]["name"],
            namespace=namespace,
            ignore_not_found=True,
        )
        if existing is not None:
            raise RuntimeError(
                "Recipe resource conflict; refusing to overwrite a preexisting object"
            )
        if (
            document["kind"] == "LeaderWorkerSet"
            and oc_get_json(
                "services",
                name=document["metadata"]["name"],
                namespace=namespace,
                ignore_not_found=True,
            )
            is not None
        ):
            raise RuntimeError("Recipe operator Service name conflicts with a preexisting object")
    # Companion first is dependency ordering only; document content/order in evidence stays intact.
    for document in sorted(documents, key=lambda item: item["kind"] != "ServingRuntime"):
        # Raw POST consumes the checked object directly: no apply annotations, namespace
        # rewriting, helper manifests, or later toolbox transformations can intervene.
        result = oc(
            "create",
            f"--raw={_resource_url(document, namespace).rsplit('/', 1)[0]}",
            "-f",
            "-",
            input_text=json.dumps(document),
            check=False,
            handled_secretly=True,
        )
        if result.returncode:
            raise RuntimeError(
                "Recipe object creation failed; no overwrite or input repair attempted"
            )
        try:
            admitted = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise RuntimeError(
                "Creation response missing ownership proof; refusing guessed cleanup"
            ) from None
        if not isinstance(admitted, dict) or not isinstance(admitted.get("metadata"), dict):
            raise RuntimeError(
                "Creation response missing ownership proof; refusing guessed cleanup"
            )
        admitted_metadata = admitted["metadata"]
        if (
            admitted.get("kind") != document["kind"]
            or admitted.get("apiVersion") != document["apiVersion"]
            or admitted_metadata.get("name") != document["metadata"]["name"]
            or admitted_metadata.get("namespace") != namespace
            or not isinstance(admitted_metadata.get("uid"), str)
            or not admitted_metadata["uid"]
        ):
            raise RuntimeError(
                "Creation response missing ownership proof; refusing guessed cleanup"
            )
        created.append(admitted)


def _delete_created_resources(namespace: str, resources: list[dict]) -> None:
    failures = []
    for document in reversed(resources):
        kind = document["kind"]
        name = document["metadata"]["name"]
        uid = document["metadata"]["uid"]
        current = oc_get_json(
            _resource_type(document), name=name, namespace=namespace, ignore_not_found=True
        )
        if current is None:
            continue
        if current.get("metadata", {}).get("uid") != uid:
            failures.append(f"{kind}/{name}: UID changed; not deleted")
            continue
        # Kubernetes enforces the UID precondition atomically even if a replacement
        # appears after our read. Never delete a discovered Service or delete by selector.
        result = oc(
            "delete",
            f"--raw={_resource_url(document, namespace)}",
            "-f",
            "-",
            input_text=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": uid},
                    "propagationPolicy": "Foreground",
                }
            ),
            check=False,
            log_stdout=False,
            log_stderr=False,
            timeout_seconds=180,
        )
        if result.returncode and "(NotFound)" not in (result.stderr or ""):
            failures.append(f"{kind}/{name}: UID-preconditioned deletion failed")
        else:
            logger.info("Removed test %s/%s", kind, name)
    if failures:
        raise RuntimeError("Failed to clean up: " + "; ".join(failures))


def _capture_created_recipe(namespace: str, resources: list[dict]) -> None:
    capture_names = {
        "LeaderWorkerSet": "capture_lws_state",
        "LLMInferenceService": "capture_llmisvc_state",
    }
    for document in resources:
        kind = document["kind"]
        metadata = document["metadata"]
        current = oc_get_json(
            _resource_type(document),
            name=metadata["name"],
            namespace=namespace,
            ignore_not_found=True,
        )
        if current is None:
            continue
        if current.get("metadata", {}).get("uid") != metadata["uid"]:
            raise RuntimeError(
                "Recipe capture UID changed; refusing unrelated workload attribution"
            )
        # Preserve the established runtime-attribution locations without invoking
        # shared capture helpers that select pods by broad labels rather than UID.
        context = (
            env.NextArtifactDir(capture_names[kind]) if kind in capture_names else nullcontext()
        )
        with context:
            _capture_admitted_state(namespace, document, current)


def _capture_admitted_state(namespace: str, document: dict, current: dict) -> None:
    directory = env.ARTIFACT_DIR / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    kind = document["kind"]
    metadata = document["metadata"]
    stem = kind.lower()
    (directory / f"{stem}.yaml").write_text(
        yaml.safe_dump(current, sort_keys=False), encoding="utf-8"
    )
    if kind == "ServingRuntime":
        return
    pods = wait_recipe.owned_pods(namespace, metadata["uid"])
    (directory / f"{stem}.pods.yaml").write_text(
        yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": pods}, sort_keys=False),
        encoding="utf-8",
    )
    if kind == "Deployment":
        # The existing consumer reads these observed status fields from run.log.
        # Log flat mappings with containerID first (its established block delimiter),
        # never entire Pods, container specs, environment values or Secret objects.
        fields = ("containerID", "image", "imageID", "name", "ready")
        for pod in pods:
            for status in pod.get("status", {}).get("containerStatuses", []):
                if (
                    all(isinstance(status.get(key), str) for key in fields[:-1])
                    and type(status.get("ready")) is bool
                ):
                    logger.info(
                        "Observed run-owned Deployment container status:\n%s",
                        yaml.safe_dump({key: status[key] for key in fields}, sort_keys=False),
                    )
    # Retain controller-expanded runtime evidence separately from submitted objects.
    deployments = oc_get_json("deployments.apps", namespace=namespace).get("items", [])
    owned = [item for item in deployments if wait_recipe.owned_by(item, metadata["uid"], namespace)]
    (directory / f"{stem}.deployments.yaml").write_text(
        yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": owned}, sort_keys=False),
        encoding="utf-8",
    )
    for pod in pods:
        oc(
            "logs",
            pod["metadata"]["name"],
            "-n",
            namespace,
            "--all-containers=true",
            check=False,
            log_stdout=False,
            log_stderr=False,
            stdout_dest=directory / f"{stem}.{pod['metadata']['name']}.log",
        )


def _recipe_http_port(recipe: dict, primary: dict) -> int:
    serving = recipe.get("serving", {})
    port = serving.get("port")
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Recipe serving.port must be between 1 and 65535")
        return port
    spec = primary.get("spec", {})
    template = (
        spec.get("template", {})
        if primary["kind"] == "Deployment"
        else spec.get("leaderWorkerTemplate", {}).get("leaderTemplate", {})
    )
    ports = [
        entry
        for container in template.get("spec", {}).get("containers", [])
        for entry in container.get("ports", [])
        if entry.get("protocol", "TCP") == "TCP"
    ]
    http = [entry for entry in ports if entry.get("name") == "http"]
    candidates = {entry.get("containerPort") for entry in (http or ports)}
    if len(candidates) != 1:
        raise ValueError(
            "Service-free endpoint requires serving.port or one unambiguous source containerPort"
        )
    port = candidates.pop()
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Source containerPort must be between 1 and 65535")
    return port


def _write_launch_manifest(name: str, manifest: dict) -> Path:
    if manifest.get("kind") not in RESOURCE_TYPES:
        raise ValueError("Unsupported launch artifact kind")
    # Artifact names are not workload names. Keep a valid authored name from
    # overwriting the fixed contract files (notably LLMISVC source-bundle).
    if name in {"source-bundle", "submitted-bundle", "bundle-metadata"}:
        name = f"launch-{name}"
    if len(name + ".yaml") > 255:
        name = manifest["kind"].lower() + "-launch"
    path = env.ARTIFACT_DIR / "src" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def _unique_name(value: str) -> str:
    return f"{slugify_identifier(value, max_length=46)}-{uuid.uuid4().hex[:8]}"


def fournos_resolve_hardware_request(hardware_spec: dict):
    """Preserve explicit /gpu requests from the PR comment."""
    return hardware_spec
