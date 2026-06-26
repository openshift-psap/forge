from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from projects.cluster.toolbox.bootstrap_lws_operator import main as bootstrap_lws_operator
from projects.cluster.toolbox.cluster_deploy_operator import main as cluster_deploy_operator
from projects.cluster.toolbox.deploy_custom_catalog import main as deploy_custom_catalog
from projects.cluster.toolbox.wait_for_crds import main as wait_for_crds_command
from projects.core.dsl.utils import slugify_identifier, truncate_k8s_name
from projects.core.dsl.utils.k8s import (
    oc,
    oc_get_json,
)
from projects.core.library import env, vault
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.gpu_operator.toolbox.bootstrap_gpu_clusterpolicy import (
    main as bootstrap_gpu_clusterpolicy,
)
from projects.gpu_operator.toolbox.bootstrap_nfd_instance import main as bootstrap_nfd_instance
from projects.kserve.toolbox.prepare_hf_model_cache.main import (
    run as prepare_hf_model_cache_toolbox_run,
)
from projects.llm_d.orchestration import runtime_config
from projects.llm_d.orchestration.cleanup_phase import run as cleanup_toolbox_run
from projects.llm_d.toolbox.capture_prepare_state.main import (
    run as capture_prepare_state_toolbox_run,
)
from projects.llm_d.toolbox.ensure_gateway import main as ensure_gateway_command
from projects.rhoai.toolbox.apply_datasciencecluster import main as apply_datasciencecluster_command
from projects.rhoai.toolbox.wait_datasciencecluster_ready import (
    main as wait_datasciencecluster_ready_command,
)

logger = logging.getLogger(__name__)


def operator_spec_by_package(platform: dict[str, Any], package: str) -> dict[str, Any]:
    operators = platform["operators"]
    if isinstance(operators, dict):
        if package in operators:
            return {"package": package, **operators[package]}
        raise KeyError(f"Unknown operator package in llm_d platform config: {package}")

    for operator_spec in operators:
        if operator_spec["package"] == package:
            return operator_spec
    raise KeyError(f"Unknown operator package in llm_d platform config: {package}")


def verify_oc_access() -> None:
    oc("whoami")


def verify_cluster_version() -> None:
    platform = runtime_config.get_platform_config()
    version_info = oc("version", "-o", "json")
    payload = json.loads(version_info.stdout)

    openshift_version = (
        payload.get("openshiftVersion")
        or payload.get("releaseClientVersion")
        or payload.get("clientVersion", {}).get("gitVersion")
        or payload.get("serverVersion", {}).get("gitVersion")
        or payload.get("serverVersion", {}).get("platform")
    )
    if not openshift_version:
        raise RuntimeError("Could not determine OpenShift version from `oc version -o json`")

    minimum = platform["cluster"]["minimum_openshift_version"]
    if runtime_config.version_tuple(openshift_version) < runtime_config.version_tuple(minimum):
        raise RuntimeError(
            f"Cluster version {openshift_version} is older than the llm_d minimum {minimum}"
        )


def ensure_operator_subscription(operator_spec: dict[str, str]) -> dict[str, object]:
    return cluster_deploy_operator.run(
        package_name=operator_spec["package"],
        target_namespace=operator_spec["namespace"],
        source_name=operator_spec["source"],
        channel=operator_spec["channel"],
        source_namespace=operator_spec.get("source_namespace", "openshift-marketplace"),
        display_name=operator_spec.get("display_name", operator_spec["package"]),
        artifact_dirname_suffix=f"_{operator_spec['package']}",
    )


def deploy_rhoai_custom_catalog(*, rhoai: dict) -> int:
    custom_catalog = rhoai["custom_catalog"]
    if not custom_catalog["enabled"]:
        logger.info("RHOAI custom catalog disabled; using default catalog source")
        return 0

    if not custom_catalog.get("image"):
        raise RuntimeError("RHOAI custom catalog is enabled but no image was configured")

    return deploy_custom_catalog.run(
        catalog_source_name=custom_catalog["name"],
        catalog_namespace=custom_catalog["namespace"],
        catalog_image=custom_catalog["image"],
        display_name=custom_catalog.get("display_name", custom_catalog["name"]),
    )


def rhoai_operator_spec(
    *,
    rhoai: dict,
    operator_spec: dict[str, str],
) -> dict[str, str]:
    custom_catalog = rhoai["custom_catalog"]
    if not custom_catalog["enabled"]:
        return operator_spec

    updated_spec = dict(operator_spec)
    updated_spec["source"] = custom_catalog["name"]
    updated_spec["source_namespace"] = custom_catalog["namespace"]
    return updated_spec


def prepare_rhcl_operator() -> None:
    platform = runtime_config.get_platform_config()
    operator_spec = operator_spec_by_package(platform, "rhcl-operator")
    ensure_operator_subscription(operator_spec)


def prepare_cert_manager() -> None:
    platform = runtime_config.get_platform_config()
    operator_spec = operator_spec_by_package(platform, "openshift-cert-manager-operator")
    ensure_operator_subscription(operator_spec)


def prepare_leader_worker_set() -> None:
    logger.info("Starting Leader Worker Set preparation")
    platform = runtime_config.get_platform_config()
    operator_spec = operator_spec_by_package(platform, "leader-worker-set")
    logger.info(f"LWS operator spec: {operator_spec}")

    ensure_operator_subscription(operator_spec)
    logger.info("LWS operator subscription ensured")

    # Bootstrap the LeaderWorkerSetOperator configuration first
    # This enables the operator to install the LeaderWorkerSet workload CRDs
    logger.info("Starting LWS operator bootstrap")
    bootstrap_lws_operator.run()
    logger.info("LWS operator bootstrap completed")

    # Now wait for the LeaderWorkerSet workload CRDs that get installed after operator configuration
    wait_for_crds_command.run(
        crd_names=[operator_spec["bootstrap_crd"]],
        display_name="Leader Worker Set workload CRD",
    )
    logger.info("LWS workload CRD ready")


def prepare_nfd() -> None:
    platform = runtime_config.get_platform_config()
    operator_spec = operator_spec_by_package(platform, "nfd")
    ensure_operator_subscription(operator_spec)
    wait_for_crds_command.run(
        crd_names=[operator_spec["bootstrap_crd"]],
        display_name="NFD bootstrap CRD",
    )
    bootstrap_nfd_instance.run()


def prepare_gpu_operator() -> None:
    platform = runtime_config.get_platform_config()
    operator_spec = operator_spec_by_package(platform, "gpu-operator-certified")
    ensure_operator_subscription(operator_spec)
    wait_for_crds_command.run(
        crd_names=[operator_spec["bootstrap_crd"]],
        display_name="GPU Operator bootstrap CRD",
    )
    bootstrap_gpu_clusterpolicy.run()


def prepare_rhoai_operator() -> None:
    platform = runtime_config.get_platform_config()
    prepare_rhcl_operator()
    deploy_rhoai_custom_catalog(rhoai=platform["rhoai"])
    operator_spec = operator_spec_by_package(platform, "rhods-operator")
    operator_spec = rhoai_operator_spec(rhoai=platform["rhoai"], operator_spec=operator_spec)
    ensure_operator_subscription(operator_spec)
    ensure_required_crds_before_dsc()


def ensure_required_crds_before_dsc() -> None:
    platform = runtime_config.get_platform_config()
    rhoai = platform["rhoai"]
    wait_for_crds_command.run(
        crd_names=rhoai["required_crds_before_dsc"],
        display_name="RHOAI pre-DSC CRDs",
    )


def ensure_required_crds() -> None:
    """Ensure CRDs required after DataScienceCluster deployment"""
    platform = runtime_config.get_platform_config()
    rhoai = platform["rhoai"]
    wait_for_crds_command.run(
        crd_names=rhoai["required_crds_after_dsc"],
        display_name="RHOAI post-DSC CRDs",
    )


def apply_datasciencecluster() -> None:
    platform = runtime_config.get_platform_config()
    rhoai = platform["rhoai"]
    apply_datasciencecluster_command.run(
        datasciencecluster_name=rhoai["datasciencecluster_name"],
        namespace=rhoai["namespace"],
        components=rhoai.get("components", ["kserve"]),
    )


def wait_for_datasciencecluster_ready() -> None:
    platform = runtime_config.get_platform_config()
    rhoai = platform["rhoai"]
    wait_datasciencecluster_ready_command.run(
        datasciencecluster_name=rhoai["datasciencecluster_name"],
        namespace=rhoai["namespace"],
    )


def ensure_gateway() -> None:
    config_dir = str(runtime_config.get_config_dir())
    platform = runtime_config.get_platform_config()
    gateway = platform["gateway"]
    ensure_gateway_command.run(
        config_dir=config_dir,
        namespace=gateway["namespace"],
        name=gateway["name"],
        gateway_class_name=gateway["gateway_class_name"],
        status_address_name=gateway["status_address_name"],
        create_if_missing=gateway["create_if_missing"],
    )


def ensure_test_namespace() -> None:
    namespace = runtime_config.get_namespace()
    ensure_namespace(
        namespace,
        labels={
            "app.kubernetes.io/managed-by": "forge",
            "forge.openshift.io/project": "llm_d",
        },
    )


def cleanup_previous_run() -> None:
    namespace = runtime_config.get_namespace()
    cleanup_toolbox_run(namespace=namespace)


def validate_model_cache(
    namespace: str, model_slug: str, model_uri: str, pvc_name_prefix: str
) -> bool:
    """Validate if model cache PVC already exists and is populated.

    Args:
        namespace: Namespace where the PVC should exist
        model_slug: Sanitized model identifier for cache naming
        model_uri: Model URI for cache key generation
        pvc_name_prefix: PVC name prefix from config

    Returns:
        True if cache is ready, False if toolbox should run
    """
    logger.info("Validating model cache for model: %s", model_slug)

    # Build expected PVC name (matching toolbox logic)
    cache_key = hashlib.sha256(model_uri.encode("utf-8")).hexdigest()[:10]
    pvc_name = truncate_k8s_name(
        f"{pvc_name_prefix}-{slugify_identifier(model_slug, max_length=32)}-{cache_key}"
    )

    # Check if PVC exists
    pvc_check = oc(
        "get",
        "persistentvolumeclaim",
        pvc_name,
        "-n",
        namespace,
        "--ignore-not-found",
        "-oname",
        check=False,
        log_stdout=True,
    )

    if pvc_check.returncode != 0 or not pvc_check.stdout.strip():
        logger.info("Model cache PVC %s does not exist", pvc_name)
        return False

    logger.info("Model cache PVC %s exists", pvc_name)

    # Check if PVC has the populated label
    pvc_data = oc_get_json(
        "persistentvolumeclaim",
        name=pvc_name,
        namespace=namespace,
    )

    if not pvc_data:
        return False

    labels = pvc_data.get("metadata", {}).get("labels", {})
    is_populated = labels.get("forge.openshift.io/model-cache-populated") == "true"

    if is_populated:
        logger.info("Model cache PVC %s is labeled as populated, ready to use", pvc_name)
        return True
    else:
        logger.info("Model cache PVC %s exists but is not labeled as populated", pvc_name)
        return False


def prepare_model_cache() -> None:
    model_cache = runtime_config.get_model_cache_config()

    if not model_cache.get("enabled", False):
        logger.info("Model cache disabled")
        return

    model_uri = runtime_config.get_model_uri()

    # Skip caching for PVC-based models
    if model_uri.startswith(("pvc://", "pvc+hf://")):
        logger.info("Skipping cache for PVC-based model: %s", model_uri)
        return

    namespace = runtime_config.get_namespace()
    model_slug = runtime_config.get_model_slug()
    pvc_name_prefix = model_cache["pvc"]["name_prefix"]

    # Quick validation: check if cache is already ready
    if validate_model_cache(namespace, model_slug, model_uri, pvc_name_prefix):
        logger.info("Model cache validation passed - cache is ready")
        return

    # Cache not ready, proceed with toolbox preparation
    logger.info("Model cache validation failed - proceeding with cache preparation")

    common_args = {
        "namespace": namespace,
        "namespace_is_managed": runtime_config.get_namespace_is_managed(),
        "model_key": model_slug,
        "model_uri": model_uri,
        "pvc_size": model_cache["pvc"]["size"],
        "access_mode": model_cache["pvc"]["access_mode"],
        "storage_class_name": model_cache["pvc"].get("storage_class_name"),
        "pvc_name_prefix": pvc_name_prefix,
        "model_directory_name": model_cache["pvc"]["model_directory_name"],
    }

    if model_uri.startswith("hf://"):
        # Get HF token file path from vault
        hf_token_file_path = None
        try:
            hf_token_file_path = vault.get_vault_content_path("psap-forge-hf", "hf_token")
            if hf_token_file_path:
                logger.info(f"Using HF token file from vault: {hf_token_file_path}")
            else:
                logger.warning("No HF token file path returned from vault psap-forge-hf/hf_token")
        except Exception as e:
            logger.warning(
                f"Failed to get HF token file path from vault psap-forge-hf/hf_token: {e}"
            )

        prepare_hf_model_cache_toolbox_run(
            **common_args,
            downloader_image=model_cache["hf"]["downloader_image"],
            hf_token_file_path=hf_token_file_path,
        )
    else:
        raise ValueError(f"Unsupported model URI scheme: {model_uri}")


def verify_gpu_nodes() -> None:
    platform = runtime_config.get_platform_config()
    selector = platform["cluster"]["gpu_node_label_selector"]
    data = oc_get_json("nodes", selector=selector, ignore_not_found=True)
    items = data.get("items", []) if data else []
    if not items:
        raise RuntimeError(
            f"No GPU nodes found with selector {selector}. The llm_d smoke path requires GPUs."
        )


def capture_prepare_state() -> None:
    artifact_dir = env.ARTIFACT_DIR
    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    rhoai = platform["rhoai"]
    gateway = platform["gateway"]

    capture_prepare_state_toolbox_run(
        artifact_dir=artifact_dir,
        namespace=namespace,
        datasciencecluster_name=rhoai["datasciencecluster_name"],
        datasciencecluster_namespace=rhoai["namespace"],
        gateway_name=gateway["name"],
        gateway_namespace=gateway["namespace"],
        capture_namespace_events=platform["artifacts"]["capture_namespace_events"],
    )
