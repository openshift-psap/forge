#!/usr/bin/env python3

from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc
from projects.rhoai.toolbox.uninstall_odh import main as uninstall_odh_command

logger = logging.getLogger("DSL")

SA_NAME = "maas-deployer"
CRB_NAME = "maas-deployer-cluster-admin"
MAAS_NAMESPACES = [
    "models-as-a-service",
    "ai-tenants",
    "ai-tenant-maas",
    "ai-tenant-forge-praxis",
]


@entrypoint
def run(
    *,
    namespace: str,
):
    """Clean up MaaS deployment resources.

    Args:
        namespace: Namespace where the deployer ServiceAccount was created.
    """
    return execute_tasks(locals())


@task
def uninstall_odh(args, ctx):
    """Uninstall the Open Data Hub operator."""
    uninstall_odh_command.run()
    return "Uninstalled ODH"


@task
def delete_aitenants(args, ctx):
    """Delete all AITenant resources."""
    best_effort_oc(
        "delete", "aitenant", "--all", "-n", "ai-tenants", description="Delete AITenants"
    )
    return "Deleted AITenants"


@task
def delete_maas_model_refs(args, ctx):
    """Delete all MaaSModelRef resources cluster-wide."""
    best_effort_oc("delete", "maasmodelref", "--all", "-A", description="Delete MaaSModelRefs")
    return "Deleted MaaSModelRefs"


@task
def delete_maas_tenant_configs(args, ctx):
    """Delete all MaasTenantConfig resources cluster-wide."""
    best_effort_oc(
        "delete", "maastenantconfig", "--all", "-A", description="Delete MaasTenantConfigs"
    )
    return "Deleted MaasTenantConfigs"


@task
def delete_maas_namespaces(args, ctx):
    """Delete the MaaS-related namespaces."""
    for ns in MAAS_NAMESPACES:
        best_effort_oc(
            "delete", "namespace", ns, "--ignore-not-found", description=f"Delete namespace {ns}"
        )
    return f"Deleted namespaces: {', '.join(MAAS_NAMESPACES)}"


@task
def delete_deployer_resources(args, ctx):
    """Delete the deployer ServiceAccount and ClusterRoleBinding."""
    best_effort_oc(
        "delete",
        "clusterrolebinding",
        CRB_NAME,
        "--ignore-not-found",
        description="Delete deployer ClusterRoleBinding",
    )
    best_effort_oc(
        "delete",
        "serviceaccount",
        SA_NAME,
        "-n",
        args.namespace,
        "--ignore-not-found",
        description="Delete deployer ServiceAccount",
    )
    return "Deleted deployer SA and ClusterRoleBinding"


if __name__ == "__main__":
    run.main()
