from __future__ import annotations

import logging
from typing import Any

from projects.core.dsl.utils.k8s import oc, oc_apply, oc_resource_exists
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


def prepare() -> int:
    verify_oc_access()

    platform = runtime_config.get_platform_config()

    prepare_nfd(platform)
    prepare_gpu_operator(platform)
    prepare_kserve(platform)

    ns = runtime_config.get_namespace()
    deploy_cfg = runtime_config.get_deploy_config()
    prepare_cfg = platform.get("prepare", {})

    ensure_test_namespace(ns)
    ensure_service_account(ns, deploy_cfg)
    ensure_scc_policy(ns, prepare_cfg)
    ensure_image_pull_secret(ns, deploy_cfg, prepare_cfg)
    ensure_model_pvc(ns, deploy_cfg, prepare_cfg)

    return 0


def cleanup() -> int:
    ns = runtime_config.get_namespace()
    logger.info("Cleaning up rhaiis benchmark resources in %s", ns)

    if not oc_resource_exists("namespace", ns):
        logger.info("Namespace %s does not exist, nothing to clean up", ns)
        return 0

    oc("delete", "inferenceservice", "--all", "-n", ns, "--ignore-not-found", check=False)
    oc("delete", "servingruntime", "--all", "-n", ns, "--ignore-not-found", check=False)
    oc("delete", "job", "--all", "-n", ns, "--ignore-not-found", check=False)
    oc("delete", "pod", "--all", "-n", ns, "--ignore-not-found", check=False)

    logger.info("Cleanup complete")
    return 0


# ---------------------------------------------------------------------------
# Cluster-level: operators
# ---------------------------------------------------------------------------


def verify_oc_access() -> None:
    result = oc("whoami", check=False)
    if result.returncode != 0:
        raise RuntimeError("Cannot connect to cluster")
    logger.info("Connected to cluster as %s", result.stdout.strip())


def _operator_spec(platform: dict[str, Any], package: str) -> dict[str, Any]:
    operators = platform.get("operators", {})
    if package not in operators:
        raise KeyError(f"Unknown operator package in platform config: {package}")
    return {"package": package, **operators[package]}


def _operator_csv_exists(namespace: str, package: str) -> bool:
    result = oc(
        "get", "csv", "-n", namespace,
        "-o", "jsonpath={.items[*].metadata.name}",
        check=False, log_stdout=False,
    )
    if result.returncode != 0:
        return False
    csv_names = result.stdout.strip().split()
    return any(package in name for name in csv_names)


def _ensure_operator_subscription(operator_spec: dict[str, str]) -> None:
    package = operator_spec["package"]
    namespace = operator_spec["namespace"]

    if _operator_csv_exists(namespace, package):
        logger.info("Operator %s already installed in %s, skipping", package, namespace)
        return

    from projects.cluster.toolbox.cluster_deploy_operator import main as cluster_deploy_operator

    cluster_deploy_operator.run(
        package_name=package,
        target_namespace=namespace,
        source_name=operator_spec["source"],
        channel=operator_spec["channel"],
        source_namespace=operator_spec.get("source_namespace", "openshift-marketplace"),
        display_name=operator_spec.get("display_name", package),
        artifact_dirname_suffix=f"_{package}",
    )


def prepare_nfd(platform: dict[str, Any]) -> None:
    from projects.cluster.toolbox.wait_for_crds import main as wait_for_crds_command
    from projects.gpu_operator.toolbox.bootstrap_nfd_instance import (
        main as bootstrap_nfd_instance,
    )

    logger.info("Preparing NFD operator")
    spec = _operator_spec(platform, "nfd")
    _ensure_operator_subscription(spec)

    if spec.get("bootstrap_crd"):
        wait_for_crds_command.run(
            crd_names=[spec["bootstrap_crd"]],
            display_name="NFD bootstrap CRD",
        )

    bootstrap_nfd_instance.run()
    logger.info("NFD operator ready")


def prepare_gpu_operator(platform: dict[str, Any]) -> None:
    from projects.cluster.toolbox.wait_for_crds import main as wait_for_crds_command
    from projects.gpu_operator.toolbox.bootstrap_gpu_clusterpolicy import (
        main as bootstrap_gpu_clusterpolicy,
    )

    logger.info("Preparing GPU operator")
    spec = _operator_spec(platform, "gpu-operator-certified")
    _ensure_operator_subscription(spec)

    if spec.get("bootstrap_crd"):
        wait_for_crds_command.run(
            crd_names=[spec["bootstrap_crd"]],
            display_name="GPU Operator bootstrap CRD",
        )

    bootstrap_gpu_clusterpolicy.run()
    logger.info("GPU operator ready")


def prepare_kserve(platform: dict[str, Any]) -> None:
    from projects.rhoai.toolbox.apply_datasciencecluster import (
        main as apply_datasciencecluster_command,
    )
    from projects.rhoai.toolbox.wait_datasciencecluster_ready import (
        main as wait_datasciencecluster_ready_command,
    )

    logger.info("Preparing KServe via RHOAI")

    rhcl_spec = _operator_spec(platform, "rhcl-operator")
    _ensure_operator_subscription(rhcl_spec)

    rhoai_spec = _operator_spec(platform, "rhoai-operator")
    _ensure_operator_subscription(rhoai_spec)

    dsc = platform.get("datasciencecluster", {})
    dsc_name = dsc.get("name", "default-dsc")
    dsc_namespace = dsc.get("namespace", "redhat-ods-applications")
    dsc_components = dsc.get("components", ["kserve"])

    apply_datasciencecluster_command.run(
        datasciencecluster_name=dsc_name,
        namespace=dsc_namespace,
        components=dsc_components,
    )

    wait_datasciencecluster_ready_command.run(
        datasciencecluster_name=dsc_name,
        namespace=dsc_namespace,
    )

    logger.info("KServe ready via RHOAI")


# ---------------------------------------------------------------------------
# Per-run: namespace, SA, SCC, secrets, PVC
# ---------------------------------------------------------------------------


def ensure_test_namespace(namespace: str) -> None:
    ensure_namespace(
        namespace,
        labels={
            "app.kubernetes.io/managed-by": "forge",
            "forge.openshift.io/project": "rhaiis",
        },
    )
    logger.info("Namespace %s ready", namespace)


def ensure_service_account(namespace: str, deploy_cfg: dict[str, Any]) -> None:
    sa_name = deploy_cfg.get("service_account_name", "")
    if not sa_name:
        return

    if oc_resource_exists("serviceaccount", sa_name, namespace=namespace):
        logger.info("Service account %s already exists in %s", sa_name, namespace)
        return

    oc("create", "serviceaccount", sa_name, "-n", namespace)
    logger.info("Created service account %s in %s", sa_name, namespace)


def ensure_scc_policy(namespace: str, prepare_cfg: dict[str, Any]) -> None:
    scc = prepare_cfg.get("scc", {})
    policy = scc.get("policy", "")
    sa = scc.get("service_account", "")
    if not policy or not sa:
        logger.info("SCC policy not configured, skipping")
        return

    oc("adm", "policy", "add-scc-to-user", policy, "-z", sa, "-n", namespace, check=False)
    logger.info("Applied SCC %s to SA %s in %s", policy, sa, namespace)


def ensure_image_pull_secret(
    namespace: str, deploy_cfg: dict[str, Any], prepare_cfg: dict[str, Any]
) -> None:
    secret_name = deploy_cfg.get("image_pull_secret", "")
    if not secret_name:
        return

    if oc_resource_exists("secret", secret_name, namespace=namespace):
        logger.info("Image pull secret %s already exists in %s", secret_name, namespace)
        return

    vault_name = prepare_cfg.get("image_pull_secret", {}).get(
        "vault_name", "psap-rhaiis-image-pull"
    )
    vault_content = prepare_cfg.get("image_pull_secret", {}).get(
        "vault_content", ".dockerconfigjson"
    )

    from projects.core.library import env, vault

    try:
        dockerconfig_path = vault.get_vault_content_path(vault_name, vault_content)
    except Exception:
        logger.warning("Vault %s not available — cannot create image pull secret", vault_name)
        return

    if not dockerconfig_path or not dockerconfig_path.exists():
        logger.warning("Vault content %s/%s not found", vault_name, vault_content)
        return

    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/project": "rhaiis",
            },
        },
        "type": "kubernetes.io/dockerconfigjson",
        "data": {
            ".dockerconfigjson": _base64_encode_file(dockerconfig_path),
        },
    }

    src_dir = env.ARTIFACT_DIR / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    oc_apply(src_dir / "image-pull-secret.yaml", manifest)
    logger.info("Created image pull secret %s in %s from vault %s", secret_name, namespace, vault_name)


def _base64_encode_file(path: Any) -> str:
    import base64

    content = path.read_bytes()
    return base64.b64encode(content).decode("ascii")


def ensure_model_pvc(
    namespace: str, deploy_cfg: dict[str, Any], prepare_cfg: dict[str, Any]
) -> None:
    pvc_name = deploy_cfg.get("storage_pvc", "")
    if not pvc_name:
        return

    if oc_resource_exists("pvc", pvc_name, namespace=namespace):
        logger.info("PVC %s already exists in %s", pvc_name, namespace)
        return

    pvc_cfg = prepare_cfg.get("model_pvc", {})
    storage_class = pvc_cfg.get("storage_class", "")
    size = pvc_cfg.get("size", "300Gi")
    access_mode = pvc_cfg.get("access_mode", "ReadWriteOnce")

    if not storage_class:
        logger.warning("No storage_class configured for model PVC, skipping creation")
        return

    manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/project": "rhaiis",
                "forge.openshift.io/preserve": "true",
            },
        },
        "spec": {
            "accessModes": [access_mode],
            "resources": {"requests": {"storage": size}},
            "storageClassName": storage_class,
        },
    }

    from projects.core.library import env

    src_dir = env.ARTIFACT_DIR / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    oc_apply(src_dir / "model-pvc.yaml", manifest)
    logger.info(
        "Created PVC %s (%s, %s, %s) in %s",
        pvc_name, storage_class, size, access_mode, namespace,
    )
