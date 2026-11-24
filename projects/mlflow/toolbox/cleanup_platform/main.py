#!/usr/bin/env python3
"""
Remove the MLflow platform from an OpenShift AI cluster.

Reverses install_platform:
1. Disables the mlflowoperator component (patches to "Removed")
2. Waits for the operator pod to terminate
3. Deletes the performance-testing namespace
4. Waits for namespace to be fully gone
"""

from __future__ import annotations

import logging

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_resource_exists

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    namespace: str = "mlflow-perf-testing",
    operator_namespace: str = "redhat-ods-applications",
    datasciencecluster_name: str = "default-dsc",
) -> int:
    """Remove the MLflow platform stack (operator + namespace)."""
    execute_tasks(locals())
    return 0


@task
def disable_mlflow_operator(args, ctx):
    """Patch the DataScienceCluster to disable the mlflowoperator component"""
    if not oc_resource_exists("datasciencecluster", args.datasciencecluster_name):
        return "DataScienceCluster not found, nothing to patch"

    oc(
        "patch",
        "datasciencecluster",
        args.datasciencecluster_name,
        "--type=merge",
        "-p",
        '{"spec":{"components":{"mlflowoperator":{"managementState":"Removed"}}}}',
    )
    return "Patched DataScienceCluster: mlflowoperator -> Removed"


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_operator_terminated(args, ctx):
    """Wait for the MLflow operator pod to be gone"""
    result = oc(
        "get",
        "pods",
        "-n",
        args.operator_namespace,
        "-l",
        "app.kubernetes.io/name=mlflow-operator",
        "-o",
        "name",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "MLflow operator pod is gone"

    return (False, "Waiting for mlflow-operator pod to terminate")


@task
def delete_namespace(args, ctx):
    """Delete the performance-testing namespace"""
    if not oc_resource_exists("namespace", args.namespace):
        return f"Namespace '{args.namespace}' does not exist, nothing to delete"

    oc("delete", "namespace", args.namespace, "--wait=false")
    return f"Namespace '{args.namespace}' deletion initiated"


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_namespace_deleted(args, ctx):
    """Wait for the namespace to be fully removed"""
    if not oc_resource_exists("namespace", args.namespace):
        return f"Namespace '{args.namespace}' is fully deleted"

    result = oc(
        "get",
        "namespace",
        args.namespace,
        "-o",
        "jsonpath={.status.phase}",
        check=False,
    )
    phase = result.stdout.strip() if result.returncode == 0 else "unknown"
    return (False, f"Namespace '{args.namespace}' phase: {phase}")


@always
@task
def capture_cleanup_state(args, ctx):
    """Verify platform cleanup by capturing remaining state"""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    _capture_to_file(
        artifacts_dir / "datasciencecluster-post-cleanup.yaml",
        "get",
        "datasciencecluster",
        args.datasciencecluster_name,
        "-o",
        "yaml",
    )
    _capture_to_file(
        artifacts_dir / "remaining-operator-pods.txt",
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
        artifacts_dir / "namespace-check.txt",
        "get",
        "namespace",
        args.namespace,
        "-o",
        "wide",
    )

    return "Captured post-cleanup platform state"


def _capture_to_file(path, *oc_args: str) -> None:
    oc(*oc_args, check=False, log_stdout=False, stdout_dest=path)


if __name__ == "__main__":
    run.main()
