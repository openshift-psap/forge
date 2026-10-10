#!/usr/bin/env python3

from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc, oc, oc_resource_exists

logger = logging.getLogger("DSL")

ODH_NAMESPACES = ["opendatahub", "opendatahub-monitoring", "odh-ai-gateway-infra"]


@entrypoint
def run(
    *,
    operator_namespace: str = "openshift-operators",
    subscription_name: str = "opendatahub-operator",
):
    """Uninstall Open Data Hub operator and all its resources.

    Args:
        operator_namespace: Namespace where the ODH operator subscription lives.
        subscription_name: Name of the ODH operator subscription.
    """
    return execute_tasks(locals())


MAAS_RESOURCE_TYPES = [
    "aitenant",
    "maasmodelref",
    "maastenantconfig",
    "maassubscription",
    "maasauthpolicy",
    "inferenceservice",
    "llminferenceservice",
    "llminferenceserviceconfig",
]


@task
def delete_maas_resources(args, ctx):
    for resource_type in MAAS_RESOURCE_TYPES:
        best_effort_oc(
            "delete",
            resource_type,
            "--all",
            "-A",
            "--wait=false",
            description=f"Delete all {resource_type}",
        )
    return f"Deleted MaaS/KServe resources: {', '.join(MAAS_RESOURCE_TYPES)}"


@task
def delete_datasciencecluster(args, ctx):
    best_effort_oc(
        "delete",
        "datasciencecluster",
        "--all",
        "--wait=true",
        "--timeout=120s",
        description="Delete all DataScienceClusters",
    )
    return "Deleted DataScienceClusters"


@task
def delete_datascienceinitialization(args, ctx):
    best_effort_oc(
        "delete",
        "datascienceinitialization",
        "--all",
        "--wait=true",
        "--timeout=120s",
        description="Delete all DataScienceInitializations",
    )
    return "Deleted DataScienceInitializations"


@task
def delete_subscription(args, ctx):
    best_effort_oc(
        "delete",
        "subscription",
        args.subscription_name,
        "-n",
        args.operator_namespace,
        "--ignore-not-found",
        description="Delete ODH operator subscription",
    )
    return f"Deleted subscription {args.subscription_name}"


@task
def delete_csv(args, ctx):
    result = oc(
        "get",
        "csv",
        "-n",
        args.operator_namespace,
        "-l",
        f"operators.coreos.com/{args.subscription_name}.{args.operator_namespace}",
        "-o",
        "name",
        check=False,
        log_stdout=True,
    )
    csv_names = result.stdout.strip()
    if not csv_names:
        return "No CSV found for ODH operator"

    for csv_name in csv_names.splitlines():
        best_effort_oc(
            "delete",
            csv_name,
            "-n",
            args.operator_namespace,
            "--ignore-not-found",
            description=f"Delete {csv_name}",
        )
    return f"Deleted CSVs: {csv_names}"


@task
def delete_odh_webhooks(args, ctx):
    """Delete webhook configurations that reference ODH namespaces."""
    deleted = []
    for webhook_type in ["validatingwebhookconfigurations", "mutatingwebhookconfigurations"]:
        result = oc("get", webhook_type, "-o", "name", check=False, log_stdout=True)
        if result.returncode != 0 or not result.stdout.strip():
            continue

        for webhook_name in result.stdout.strip().splitlines():
            result = oc(
                "get",
                webhook_name,
                "-o",
                "jsonpath={.webhooks[*].clientConfig.service.namespace}",
                check=False,
                log_stdout=True,
            )
            namespaces = result.stdout.strip().split()
            if any(ns in ODH_NAMESPACES for ns in namespaces):
                best_effort_oc(
                    "delete",
                    webhook_name,
                    "--ignore-not-found",
                    description=f"Delete {webhook_name}",
                )
                deleted.append(webhook_name)

    if not deleted:
        return "No ODH webhooks found"
    return f"Deleted webhooks: {', '.join(deleted)}"


@task
def force_delete_stuck_resources(args, ctx):
    """Remove finalizers and delete resources that block namespace deletion."""
    deleted_count = 0
    for resource_type in MAAS_RESOURCE_TYPES:
        result = oc(
            "get",
            resource_type,
            "-A",
            "-o",
            'jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{"\\n"}{end}',
            check=False,
            log_stdout=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue

        for entry in result.stdout.strip().splitlines():
            ns, name = entry.split("/", 1)
            best_effort_oc(
                "patch",
                resource_type,
                name,
                "-n",
                ns,
                "--type=merge",
                '-p={"metadata":{"finalizers":null}}',
                description=f"Remove finalizers from {resource_type}/{name} in {ns}",
            )
            best_effort_oc(
                "delete",
                resource_type,
                name,
                "-n",
                ns,
                "--ignore-not-found",
                description=f"Delete {resource_type}/{name} in {ns}",
            )
            deleted_count += 1

    if not deleted_count:
        return "No stuck resources found"
    return f"Force-deleted {deleted_count} stuck resources"


@task
def delete_odh_namespaces(args, ctx):
    deleted = []
    for ns in ODH_NAMESPACES:
        if not oc_resource_exists("namespace", ns):
            continue

        best_effort_oc(
            "delete",
            "all",
            "--all",
            "-n",
            ns,
            "--wait=true",
            "--timeout=60s",
            description=f"Delete all resources in {ns}",
        )
        best_effort_oc(
            "delete",
            "namespace",
            ns,
            "--wait=true",
            "--timeout=180s",
            description=f"Delete namespace {ns}",
        )
        deleted.append(ns)

    if not deleted:
        return "No ODH namespaces to delete"
    return f"Deleted namespaces: {', '.join(deleted)}"


@task
def delete_odh_crds(args, ctx):
    result = oc(
        "get",
        "crd",
        "-l",
        "app.kubernetes.io/part-of=opendatahub-operator",
        "-o",
        "name",
        check=False,
        log_stdout=True,
    )
    crd_names = result.stdout.strip()
    if not crd_names:
        return "No ODH CRDs found"

    best_effort_oc(
        "delete",
        "crd",
        "-l",
        "app.kubernetes.io/part-of=opendatahub-operator",
        "--wait=true",
        "--timeout=120s",
        description="Delete ODH CRDs",
    )
    return "Deleted ODH CRDs"


if __name__ == "__main__":
    run.main()
