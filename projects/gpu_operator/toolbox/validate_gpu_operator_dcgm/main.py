#!/usr/bin/env python3

from __future__ import annotations

import json
import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger(__name__)

DCGM_SERVICEMONITOR_NAME = "nvidia-dcgm-exporter"


@entrypoint
def run(
    *,
    clusterpolicy_name: str = "gpu-cluster-policy",
    namespace: str | None = None,
) -> int:
    """
    Validate that the GPU Operator DCGM exporter and its ServiceMonitor are enabled.

    Args:
        clusterpolicy_name: Name of the ClusterPolicy resource
        namespace: GPU operator namespace (derived from ClusterPolicy status if not set)
    """

    execute_tasks(locals())
    return 0


@task
def resolve_namespace(args, ctx):
    """Resolve the GPU operator namespace from ClusterPolicy status"""

    if args.namespace:
        ctx.namespace = args.namespace
        return f"Using provided namespace: {ctx.namespace}"

    result = oc(
        "get",
        f"clusterpolicy/{args.clusterpolicy_name}",
        "-o",
        "json",
        check=False,
        stdout_dest=args.artifact_dir / "clusterpolicy.json",
    )
    if not result.success:
        raise RuntimeError(f"ClusterPolicy '{args.clusterpolicy_name}' not found on the cluster")

    payload = json.loads(result.stdout)
    ns = payload.get("status", {}).get("namespace")
    if not ns:
        raise RuntimeError(f"ClusterPolicy '{args.clusterpolicy_name}' has no status.namespace")

    ctx.namespace = ns
    return f"Resolved namespace from ClusterPolicy status: {ctx.namespace}"


@task
def check_dcgm_exporter_enabled(args, ctx):
    """Check that dcgmExporter is enabled in ClusterPolicy"""

    result = oc(
        "get",
        f"clusterpolicy/{args.clusterpolicy_name}",
        "-o",
        "jsonpath={.spec.dcgmExporter.enabled}",
        check=False,
    )
    if not result.success:
        raise RuntimeError(f"ClusterPolicy '{args.clusterpolicy_name}' not found on the cluster")

    value = result.stdout.strip()
    if value != "true":
        raise RuntimeError(
            f"DCGM exporter is not enabled in ClusterPolicy (spec.dcgmExporter.enabled = {value!r})"
        )

    return f"dcgmExporter.enabled = {value}"


@task
def check_dcgm_service_monitor_enabled(args, ctx):
    """Check that dcgmExporter.serviceMonitor is enabled in ClusterPolicy"""

    result = oc(
        "get",
        f"clusterpolicy/{args.clusterpolicy_name}",
        "-o",
        "jsonpath={.spec.dcgmExporter.serviceMonitor.enabled}",
        check=False,
    )

    value = result.stdout.strip()
    if value != "true":
        raise RuntimeError(
            f"DCGM ServiceMonitor is not enabled in ClusterPolicy "
            f"(spec.dcgmExporter.serviceMonitor.enabled = {value!r})"
        )

    return f"dcgmExporter.serviceMonitor.enabled = {value}"


@task
def check_dcgm_service_monitor_exists(args, ctx):
    """Check that the DCGM ServiceMonitor resource exists"""

    result = oc(
        "get",
        f"servicemonitor/{DCGM_SERVICEMONITOR_NAME}",
        "-n",
        ctx.namespace,
        check=False,
    )
    if not result.success:
        raise RuntimeError(
            f"ServiceMonitor '{DCGM_SERVICEMONITOR_NAME}' not found in namespace '{ctx.namespace}'"
        )

    return f"ServiceMonitor {DCGM_SERVICEMONITOR_NAME} exists in {ctx.namespace}"


if __name__ == "__main__":
    run.main()
