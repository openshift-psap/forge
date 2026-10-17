import json
import logging
from pathlib import Path

from projects.ag_praxis.toolbox.cleanup_maas import main as cleanup_maas_command
from projects.cluster.toolbox.wait_for_crds import main as wait_for_crds_command
from projects.core.dsl.utils.k8s import oc, oc_get_json
from projects.core.library import config
from projects.rhoai.library.deploy import (
    prepare_rhoai_operator as rhoai_prepare_rhoai_operator,
)
from projects.rhoai.toolbox.apply_datasciencecluster import main as apply_datasciencecluster_command
from projects.rhoai.toolbox.wait_datasciencecluster_ready import (
    main as wait_datasciencecluster_ready_command,
)

logger = logging.getLogger(__name__)
RHOAI_ICSP_MANIFEST = Path(__file__).resolve().parent / "manifests" / "rhoai-registry-icsp.yaml"


def _register_namespace_in_gateway(namespace, gateway_name, gateway_namespace):
    """Patch the Gateway to allow routes from the given namespace, preserving existing values."""

    gateway = oc_get_json("gateway", name=gateway_name, namespace=gateway_namespace)
    listeners = gateway.get("spec", {}).get("listeners", [])
    if not listeners:
        raise RuntimeError(f"Gateway {gateway_name} has no listeners")

    listener = listeners[0]
    allowed = listener.get("allowedRoutes", {}).get("namespaces", {})

    if allowed.get("from") == "All":
        logger.info(f"Gateway {gateway_name} already allows all namespaces")
        return

    selector = allowed.get("selector", {})
    match_expressions = selector.get("matchExpressions", [])

    target_expr = None
    for expr in match_expressions:
        if expr.get("key") == "kubernetes.io/metadata.name" and expr.get("operator") == "In":
            target_expr = expr
            break

    if target_expr is not None:
        existing_values = target_expr.get("values", [])
        if namespace in existing_values:
            logger.info(f"Namespace {namespace} already registered in gateway {gateway_name}")
            return
        target_expr["values"] = existing_values + [namespace]
    else:
        match_expressions.append(
            {
                "key": "kubernetes.io/metadata.name",
                "operator": "In",
                "values": [namespace],
            }
        )
        selector["matchExpressions"] = match_expressions

    allowed["from"] = "Selector"
    allowed["selector"] = selector

    patch = [
        {
            "op": "replace",
            "path": "/spec/listeners/0/allowedRoutes/namespaces",
            "value": allowed,
        }
    ]

    oc(
        "patch",
        "gateway",
        gateway_name,
        "-n",
        gateway_namespace,
        "--type=json",
        f"-p={json.dumps(patch)}",
    )
    logger.info(f"Registered namespace {namespace} in gateway {gateway_name}")


def apply_rhoai_icsp() -> None:
    logger.info("Applying RHOAI ICSP manifest: %s", RHOAI_ICSP_MANIFEST)
    oc("apply", "-f", str(RHOAI_ICSP_MANIFEST))


def prepare_rhoai_operator() -> None:
    platform = config.project.get_config("platform")
    rhoai_prepare_rhoai_operator(
        platform=platform,
        rhoai=platform["rhoai"],
        icsp_applier=apply_rhoai_icsp,
    )


def apply_datasciencecluster() -> None:
    platform = config.project.get_config("platform")
    rhoai = platform["rhoai"]
    apply_datasciencecluster_command.run(
        datasciencecluster_name=rhoai["datasciencecluster_name"],
        namespace=rhoai["namespace"],
        components=rhoai.get("components", ["kserve"]),
    )


def wait_for_datasciencecluster_ready() -> None:
    platform = config.project.get_config("platform")
    rhoai = platform["rhoai"]
    wait_datasciencecluster_ready_command.run(
        datasciencecluster_name=rhoai["datasciencecluster_name"],
        namespace=rhoai["namespace"],
    )


def ensure_required_crds() -> None:
    platform = config.project.get_config("platform")
    rhoai = platform["rhoai"]
    crds = rhoai.get("required_crds_after_dsc", [])
    if not crds:
        logger.info("No post-DSC CRDs to wait for")
        return
    wait_for_crds_command.run(
        crd_names=crds,
        display_name="RHOAI post-DSC CRDs",
    )


def prepare():
    logger.info("=== AG Praxis Project Prepare Phase ===")

    from projects.cluster.library.prom.collection import prepare_user_workload_monitoring

    prepare_user_workload_monitoring(during="prepare")

    prepare_rhoai_operator()
    apply_datasciencecluster()
    wait_for_datasciencecluster_ready()
    ensure_required_crds()

    maas_cfg = config.project.get_config("platform.maas")

    from projects.ag_praxis.toolbox.deploy_maas import main as deploy_maas_command

    deploy_maas_command.run(
        namespace=maas_cfg["namespace"],
        repo_url=maas_cfg["repo_url"],
        repo_ref=maas_cfg["repo_ref"],
        operator_type=maas_cfg["operator_type"],
    )

    test_namespace = config.project.get_config("test.namespace")

    gateway_cfg = maas_cfg["gateway"]
    _register_namespace_in_gateway(
        test_namespace,
        gateway_name=gateway_cfg["name"],
        gateway_namespace=gateway_cfg["namespace"],
    )


def cleanup():
    logger.info("=== AG Praxis Project Cleanup Phase ===")

    maas_cfg = config.project.get_config("platform.maas")

    cleanup_maas_command.run(
        namespace=maas_cfg.get("namespace", "default"),
    )
