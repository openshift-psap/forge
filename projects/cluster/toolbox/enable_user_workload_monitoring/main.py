#!/usr/bin/env python3

"""
Enable User Workload Monitoring Toolbox

Ensures that user workload monitoring is enabled on the OpenShift cluster
by patching the cluster-monitoring-config ConfigMap.
"""

from __future__ import annotations

import json
import logging

import yaml

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger("DSL")

MONITORING_NAMESPACE = "openshift-monitoring"
UWM_NAMESPACE = "openshift-user-workload-monitoring"
CONFIGMAP_NAME = "cluster-monitoring-config"


@entrypoint
def run() -> int:
    """
    Enable user workload monitoring on the cluster.

    Patches the cluster-monitoring-config ConfigMap in openshift-monitoring
    to set enableUserWorkload: true.
    """
    execute_tasks(locals())
    return 0


@task
def check_current_state(args, ctx):
    """Check whether user workload monitoring is already enabled."""

    result = oc(
        "-n",
        MONITORING_NAMESPACE,
        "get",
        "configmap",
        CONFIGMAP_NAME,
        "-o",
        "jsonpath={.data.config\\.yaml}",
        check=False,
    )

    ctx.configmap_exists = result.success
    ctx.current_config = {}

    if result.success and result.stdout.strip():
        try:
            parsed = yaml.safe_load(result.stdout)
        except yaml.YAMLError:
            parsed = None

        ctx.current_config = parsed if isinstance(parsed, dict) else {}

    ctx.already_enabled = bool(ctx.current_config.get("enableUserWorkload", False))

    if ctx.already_enabled:
        return "User workload monitoring is already enabled"
    return "User workload monitoring is not yet enabled"


@task
def enable_user_workload_monitoring(args, ctx):
    """Patch or create the ConfigMap to enable user workload monitoring."""

    if ctx.already_enabled:
        return "Already enabled, nothing to do"

    ctx.current_config["enableUserWorkload"] = True
    new_config_yaml = yaml.dump(ctx.current_config, default_flow_style=False)

    if ctx.configmap_exists:
        patch = json.dumps({"data": {"config.yaml": new_config_yaml}})
        oc(
            "-n",
            MONITORING_NAMESPACE,
            "patch",
            "configmap",
            CONFIGMAP_NAME,
            "--type=merge",
            f"-p={patch}",
        )
    else:
        oc(
            "-n",
            MONITORING_NAMESPACE,
            "create",
            "configmap",
            CONFIGMAP_NAME,
            f"--from-literal=config.yaml={new_config_yaml}",
        )

    return "Enabled user workload monitoring in cluster-monitoring-config"


@retry(attempts=30, delay=10)
@task
def wait_for_prometheus_operator(args, ctx):
    """Wait for the prometheus-operator deployment to be available in the UWM namespace."""

    result = oc(
        "-n",
        UWM_NAMESPACE,
        "get",
        "deployment",
        "prometheus-operator",
        "-o",
        "jsonpath={.status.availableReplicas}",
        check=False,
    )

    if not result.success:
        return (False, "prometheus-operator deployment not found yet")

    available = result.stdout.strip()
    if not available or int(available) == 0:
        return (False, f"prometheus-operator not yet available (availableReplicas={available!r})")

    return "prometheus-operator is available in openshift-user-workload-monitoring"


if __name__ == "__main__":
    run.main()
