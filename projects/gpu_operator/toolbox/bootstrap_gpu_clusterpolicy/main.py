#!/usr/bin/env python3

from __future__ import annotations

import json
import logging

from projects.core.dsl import (
    always,
    entrypoint,
    execute_tasks,
    on_failure,
    retry,
    shell,
    task,
    template,
)
from projects.core.dsl.utils.k8s import oc, oc_resource_exists

from .on_failure_helpers import generate_failure_analysis

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    clusterpolicy_name: str = "gpu-cluster-policy",
    timeout_seconds: int = 1800,
) -> int:
    """
    Bootstrap the NVIDIA ClusterPolicy used by llm_d and wait for readiness.

    Args:
        clusterpolicy_name: Name of the ClusterPolicy resource
        timeout_seconds: Maximum time to wait for the ClusterPolicy to become ready
    """

    execute_tasks(locals())
    return 0


@task
def setup_directories(args, ctx):
    """Create command source and artifact directories"""

    shell.mkdir("src")
    shell.mkdir("artifacts")
    return f"Prepared GPU ClusterPolicy bootstrap for {args.clusterpolicy_name}"


@task
def render_manifest(args, ctx):
    """Render the ClusterPolicy manifest"""

    ctx.manifest_file = args.artifact_dir / "src" / "gpu-clusterpolicy.yaml"
    template.render_template_to_file("gpu-clusterpolicy.yaml.j2", ctx.manifest_file)
    return f"Rendered ClusterPolicy manifest for {args.clusterpolicy_name}"


@task
def apply_manifest_if_missing(args, ctx):
    """Apply the ClusterPolicy manifest when missing"""

    if oc_resource_exists("clusterpolicy", args.clusterpolicy_name):
        return f"ClusterPolicy/{args.clusterpolicy_name} already exists"

    shell.run(f"oc apply -f {ctx.manifest_file}")
    return f"Applied ClusterPolicy/{args.clusterpolicy_name}"


@task
def get_gpu_operator_namespace(args, ctx):
    """Get the GPU operator namespace from ClusterPolicy status"""

    result = oc(
        "get",
        f"clusterpolicy/{args.clusterpolicy_name}",
        "-ojsonpath={.status.namespace}",
        check=False,
        log_stdout=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        # Fallback to default namespace if ClusterPolicy doesn't have status yet
        ctx.gpu_operator_namespace = "nvidia-gpu-operator"
        logger.warning(
            f"Could not get namespace from ClusterPolicy, using default: {ctx.gpu_operator_namespace}"
        )
    else:
        ctx.gpu_operator_namespace = result.stdout.strip()
        logger.info(f"Using GPU operator namespace: {ctx.gpu_operator_namespace}")

    return f"GPU operator namespace: {ctx.gpu_operator_namespace}"


@on_failure(generate_failure_analysis)
@retry(attempts=60, delay=15, backoff=1.0)
@task
def wait_for_clusterpolicy_ready(args, ctx):
    """Wait for the ClusterPolicy to report ready"""

    result = oc(
        "get",
        "clusterpolicy",
        args.clusterpolicy_name,
        "-o",
        "json",
        check=False,
        log_stdout=False,
    )
    if result.returncode != 0:
        ctx.clusterpolicy_error_message = (
            f"Failed to get ClusterPolicy {args.clusterpolicy_name}: {result.stderr}"
        )
        return False  # Retry

    try:
        payload = json.loads(result.stdout)
        status = payload.get("status", {})
        state = status.get("state", "").lower()

        if state == "ready":
            return f"ClusterPolicy/{args.clusterpolicy_name} ready"

        # Look for error conditions to provide useful feedback
        conditions = status.get("conditions", [])
        for condition in conditions:
            if condition.get("type") == "Error" and condition.get("status") == "True":
                message = condition.get("message", "")
                if message:
                    logger.info(f"ClusterPolicy not ready: {message}")
                    ctx.clusterpolicy_error_message = message
                    ctx.clusterpolicy_full_status = status
                    return (False, f"ClusterPolicy not ready: {message}")

        # Fallback if no specific error message
        error_msg = f"ClusterPolicy state: {state}"
        ctx.clusterpolicy_error_message = error_msg
        ctx.clusterpolicy_full_status = status
        return (False, error_msg)

    except json.JSONDecodeError as e:
        ctx.clusterpolicy_error_message = f"Failed to parse ClusterPolicy JSON response: {e}"
        return False  # Retry on JSON parse error


@always
@task
def capture_clusterpolicy_state(args, ctx):
    """Capture ClusterPolicy YAML for diagnostics"""

    shell.run(
        f"oc get clusterpolicy {args.clusterpolicy_name} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts" / "clusterpolicy.yaml",
        check=False,
    )
    return f"Captured ClusterPolicy/{args.clusterpolicy_name} state"


@always
@task
def capture_gpu_operator_resources(args, ctx):
    """Capture DaemonSets and Pods state in the GPU operator namespace"""

    # Get the namespace, with fallback if not set
    namespace = getattr(ctx, "gpu_operator_namespace", "nvidia-gpu-operator")

    # Capture all DaemonSets in the GPU operator namespace
    shell.run(
        f"oc get daemonsets -n {namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts" / "gpu-operator-daemonsets.yaml",
        check=False,
    )

    # Capture all Pods in the GPU operator namespace
    shell.run(
        f"oc get pods -n {namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts" / "gpu-operator-pods.yaml",
        check=False,
    )

    # Capture DaemonSet status
    shell.run(
        f"oc get daemonsets -n {namespace} -owide",
        stdout_dest=args.artifact_dir / "artifacts" / "gpu-operator-daemonsets.status.txt",
        check=False,
    )

    # Capture DaemonSet descriptions
    shell.run(
        f"oc describe daemonsets -n {namespace}",
        stdout_dest=args.artifact_dir / "artifacts" / "gpu-operator-daemonsets.describe.txt",
        check=False,
    )

    # Capture Pod status
    shell.run(
        f"oc get pods -n {namespace} -owide",
        stdout_dest=args.artifact_dir / "artifacts" / "gpu-operator-pods.status.txt",
        check=False,
    )

    # Capture Pod descriptions
    shell.run(
        f"oc describe pods -n {namespace}",
        stdout_dest=args.artifact_dir / "artifacts" / "gpu-operator-pods.describe.txt",
        check=False,
    )

    return f"Captured DaemonSets and Pods state in {namespace}"


if __name__ == "__main__":
    run.main()
