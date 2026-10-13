import json
import logging

from projects.ag_praxis.toolbox.cleanup_maas import main as cleanup_maas_command
from projects.ag_praxis.toolbox.deploy_maas import main as deploy_maas_command
from projects.core.dsl.utils.k8s import oc, oc_get_json
from projects.core.library import config

logger = logging.getLogger(__name__)


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


def prepare():
    logger.info("=== AG Praxis Project Prepare Phase ===")

    maas_cfg = config.project.get_config("platform.maas")

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

    from projects.llm_d.toolbox.ensure_gateway import main as ensure_gateway_command

    sim_gw = config.project.get_config("platform.sim.gateway")
    ensure_gateway_command.run(
        config_dir="",
        namespace=sim_gw["namespace"],
        name=sim_gw["name"],
        gateway_class_name=sim_gw["gateway_class_name"],
        status_address_name=sim_gw["status_address_name"],
        create_if_missing=sim_gw["create_if_missing"],
    )


def cleanup():
    logger.info("=== AG Praxis Project Cleanup Phase ===")

    maas_cfg = config.project.get_config("platform.maas")

    cleanup_maas_command.run(
        namespace=maas_cfg["namespace"],
    )
