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
    labels = platform.get("labels", {})

    ensure_test_namespace(ns, labels)
    ensure_service_account(ns, deploy_cfg)
    ensure_scc_policy(ns, deploy_cfg, prepare_cfg)
    ensure_image_pull_secret(ns, deploy_cfg, prepare_cfg)
    ensure_model_pvc(ns, deploy_cfg, prepare_cfg, labels)

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


def _operator_csv_succeeded(namespace: str, package: str) -> bool:
    result = oc(
        "get",
        "csv",
        "-n",
        namespace,
        "-o",
        r'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\n"}{end}',
        check=False,
        log_stdout=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.strip().splitlines():
        name, _, phase = line.partition("\t")
        if package in name and phase == "Succeeded":
            return True
    return False


def _ensure_operator_subscription(operator_spec: dict[str, str]) -> None:
    package = operator_spec["package"]
    namespace = operator_spec["namespace"]

    if _operator_csv_succeeded(namespace, package):
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


def ensure_test_namespace(namespace: str, labels: dict[str, str]) -> None:
    ensure_namespace(namespace, labels=labels)
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


def ensure_scc_policy(
    namespace: str, deploy_cfg: dict[str, Any], prepare_cfg: dict[str, Any]
) -> None:
    scc = prepare_cfg.get("scc", {})
    policy = scc.get("policy")
    sa = deploy_cfg.get("service_account_name", "")
    if not policy:
        logger.info("SCC policy not configured, skipping")
        return
    if not sa:
        logger.info("No service account configured, skipping SCC")
        return

    oc("adm", "policy", "add-scc-to-user", policy, "-z", sa, "-n", namespace)
    logger.info("Applied SCC %s to SA %s in %s", policy, sa, namespace)


def ensure_image_pull_secret(
    namespace: str, deploy_cfg: dict[str, Any], prepare_cfg: dict[str, Any]
) -> None:
    secret_name = deploy_cfg.get("image_pull_secret", "")
    if not secret_name:
        logger.info("No image pull secret configured, skipping")
        return

    if oc_resource_exists("secret", secret_name, namespace=namespace):
        logger.info("Image pull secret %s already exists in %s", secret_name, namespace)
        return

    vault_cfg = prepare_cfg.get("image_pull_secret", {})
    vault_name = vault_cfg.get("vault_name")
    vault_content = vault_cfg.get("vault_content")

    if not vault_name or not vault_content:
        raise RuntimeError(
            f"Image pull secret {secret_name} is configured but "
            "platform.prepare.image_pull_secret.vault_name/vault_content are missing"
        )

    from projects.core.library import vault

    try:
        dockerconfig_path = vault.get_vault_content_path(vault_name, vault_content)
    except Exception as exc:
        raise RuntimeError(
            f"Vault {vault_name} not available — cannot create image pull secret {secret_name}"
        ) from exc

    if not dockerconfig_path or not dockerconfig_path.exists():
        raise FileNotFoundError(f"Vault content {vault_name}/{vault_content} not found")

    oc(
        "create",
        "secret",
        "generic",
        secret_name,
        f"--from-file=.dockerconfigjson={dockerconfig_path}",
        "--type=kubernetes.io/dockerconfigjson",
        "-n",
        namespace,
    )
    logger.info(
        "Created image pull secret %s in %s from vault %s", secret_name, namespace, vault_name
    )


def ensure_model_pvc(
    namespace: str,
    deploy_cfg: dict[str, Any],
    prepare_cfg: dict[str, Any],
    labels: dict[str, str],
) -> None:
    pvc_name = deploy_cfg.get("storage_pvc", "")
    if not pvc_name:
        return

    if oc_resource_exists("pvc", pvc_name, namespace=namespace):
        logger.info("PVC %s already exists in %s", pvc_name, namespace)
        return

    pvc_cfg = prepare_cfg.get("model_pvc", {})
    storage_class = pvc_cfg.get("storage_class")
    size = pvc_cfg["size"]
    access_mode = pvc_cfg["access_mode"]

    pvc_spec: dict[str, Any] = {
        "accessModes": [access_mode],
        "resources": {"requests": {"storage": size}},
    }
    if storage_class:
        pvc_spec["storageClassName"] = storage_class

    manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name,
            "namespace": namespace,
            "labels": {
                **labels,
                "forge.openshift.io/preserve": "true",
            },
        },
        "spec": pvc_spec,
    }

    from projects.core.library import env

    src_dir = env.ARTIFACT_DIR / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    oc_apply(src_dir / "model-pvc.yaml", manifest)
    logger.info(
        "Created PVC %s (%s, %s, %s) in %s",
        pvc_name,
        storage_class,
        size,
        access_mode,
        namespace,
    )
