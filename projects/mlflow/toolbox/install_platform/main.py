#!/usr/bin/env python3
"""
Install the MLflow platform on an OpenShift AI (RHOAI) cluster.

Assumes RHOAI is already installed. This toolbox:
1. Verifies the DataScienceCluster exists
2. Enables the mlflowoperator component (patches to "Managed")
3. Waits for the MLflow operator pod to be running
4. Creates the performance-testing namespace with the mlflow-enabled label
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import oc, oc_apply, oc_resource_exists

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    namespace: str = "mlflow-perf-testing",
    datasciencecluster_name: str = "default-dsc",
    operator_namespace: str = "redhat-ods-applications",
    mlflow_operator_image: str = "quay.io/opendatahub/mlflow-operator:odh-stable",
) -> int:
    """
    Install the MLflow platform stack.

    Args:
        namespace: Namespace to create for MLflow workloads
        datasciencecluster_name: DataScienceCluster to enable MLflow on
        operator_namespace: Namespace containing RHOAI-managed MLflow pods
        mlflow_operator_image: Image for the MLflow operator controller.
    """
    execute_tasks(locals())
    return 0


@task
def verify_rhoai_installed(args, ctx):
    """Verify that OpenShift AI (DataScienceCluster) is installed"""
    if not oc_resource_exists("datasciencecluster", args.datasciencecluster_name):
        raise RuntimeError(
            f"DataScienceCluster {args.datasciencecluster_name!r} not found. "
            "RHOAI must be installed before running this toolbox."
        )

    result = oc(
        "get",
        "datasciencecluster",
        args.datasciencecluster_name,
        "-o",
        "jsonpath={.spec.components.mlflowoperator.managementState}",
        check=False,
    )
    current_state = result.stdout.strip() if result.returncode == 0 else "unknown"
    ctx.mlflow_was_managed = current_state == "Managed"

    logger.info("DataScienceCluster found, mlflowoperator state: %s", current_state)
    return f"RHOAI verified, mlflowoperator currently '{current_state}'"


@task
def enable_mlflow_operator(args, ctx):
    """Patch the DataScienceCluster to enable the mlflowoperator component"""
    if ctx.mlflow_was_managed:
        return "MLflow operator already Managed, skipping patch"

    oc(
        "patch",
        "datasciencecluster",
        args.datasciencecluster_name,
        "--type=merge",
        "-p",
        '{"spec":{"components":{"mlflowoperator":{"managementState":"Managed"}}}}',
    )

    return "Patched DataScienceCluster: mlflowoperator -> Managed"


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_mlflow_operator_ready(args, ctx):
    """Wait for the MLflow operator pod to be running in redhat-ods-applications"""
    result = oc(
        "get",
        "pods",
        "-n",
        args.operator_namespace,
        "-l",
        "app.kubernetes.io/name=mlflow-operator",
        "-o",
        "jsonpath={.items[0].status.phase}",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return (False, "Waiting for mlflow-operator pod to appear")

    phase = result.stdout.strip()
    if phase == "Running":
        return "MLflow operator pod is Running"

    return (False, f"MLflow operator pod phase: {phase}")


@task
def ensure_mlflow_operator_image(args, ctx):
    """Set and verify the configured MLflow operator controller image."""
    deployment = "deployment/mlflow-operator-controller-manager"
    container = "manager"
    image_result = oc(
        "get",
        deployment,
        "-n",
        args.operator_namespace,
        "-o",
        'jsonpath={.spec.template.spec.containers[?(@.name=="manager")].image}',
        check=False,
    )
    current_image = image_result.stdout.strip() if image_result.returncode == 0 else ""
    if current_image != args.mlflow_operator_image:
        oc(
            "set",
            "image",
            deployment,
            f"{container}={args.mlflow_operator_image}",
            "-n",
            args.operator_namespace,
        )
    oc(
        "patch",
        deployment,
        "-n",
        args.operator_namespace,
        "--type=strategic",
        "-p",
        '{"spec":{"template":{"spec":{"containers":[{"name":"manager","imagePullPolicy":"Always"}]}}}}',
    )

    oc(
        "rollout",
        "status",
        deployment,
        "-n",
        args.operator_namespace,
        "--timeout=180s",
        timeout_seconds=210,
    )
    image_result = oc(
        "get",
        deployment,
        "-n",
        args.operator_namespace,
        "-o",
        'jsonpath={.spec.template.spec.containers[?(@.name=="manager")].image}',
        check=False,
    )
    actual_image = image_result.stdout.strip() if image_result.returncode == 0 else ""
    if actual_image != args.mlflow_operator_image:
        raise RuntimeError(
            "MLflow operator image mismatch after rollout: "
            f"expected {args.mlflow_operator_image}, got {actual_image or 'unknown'}"
        )
    return f"MLflow operator image ready: {actual_image}"


@task
def create_namespace(args, ctx):
    """Create the mlflow-perf-testing namespace with mlflow-enabled label"""
    manifest = yaml.safe_load(
        render_template("00-namespace.yaml.j2", {"namespace": args.namespace})
    )
    oc_apply(args.artifact_dir / "namespace.yaml", manifest)
    return f"Namespace '{args.namespace}' created"


@always
@task
def capture_platform_state(args, ctx):
    """Capture platform component states for diagnostics"""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    _capture_to_file(
        artifacts_dir / "datasciencecluster.yaml",
        "get",
        "datasciencecluster",
        args.datasciencecluster_name,
        "-o",
        "yaml",
    )
    _capture_to_file(
        artifacts_dir / "mlflow-operator-pods.txt",
        "get",
        "pods",
        "-n",
        args.operator_namespace,
        "-l",
        "app.kubernetes.io/name=mlflow-operator",
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "namespace.yaml",
        "get",
        "namespace",
        args.namespace,
        "-o",
        "yaml",
    )

    return "Captured platform state"


def _capture_to_file(path: Path, *oc_args: str) -> None:
    oc(*oc_args, check=False, log_stdout=False, stdout_dest=path)


if __name__ == "__main__":
    run.main()
