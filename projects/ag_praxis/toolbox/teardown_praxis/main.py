#!/usr/bin/env python3

from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc, oc

logger = logging.getLogger("DSL")


@entrypoint
def run(
    *,
    namespace: str,
    deployment_name: str = "praxis",
):
    """Capture Praxis state and tear down the deployment.

    Owned resources (ConfigMap, Service, ServiceMonitor) are garbage-collected
    when the Deployment is deleted.

    Args:
        namespace: Namespace where Praxis is deployed.
        deployment_name: Name of the Praxis Deployment.
    """
    execute_tasks(locals())


@task
def capture_deployment(args, ctx):
    """Capture the Deployment YAML."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "get",
        "deployment",
        args.deployment_name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "deployment.yaml",
    )
    return f"Captured deployment {args.deployment_name}"


@task
def capture_replicasets(args, ctx):
    """Capture ReplicaSet YAML for the Praxis deployment."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "get",
        "replicaset",
        "-n",
        args.namespace,
        "-l",
        f"app={args.deployment_name}",
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "replicasets.yaml",
    )
    return "Captured ReplicaSets"


@task
def capture_pods(args, ctx):
    """Capture Pod YAML and logs for each Praxis pod."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = artifacts_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        f"app={args.deployment_name}",
        "-o",
        "jsonpath={.items[*].metadata.name}",
        check=False,
        log_stdout=True,
    )
    pod_names = result.stdout.strip().split() if result.stdout.strip() else []

    oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        f"app={args.deployment_name}",
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "pods.yaml",
    )

    for pod_name in pod_names:
        oc(
            "logs",
            pod_name,
            "-n",
            args.namespace,
            "--all-containers",
            check=False,
            log_stdout=False,
            stdout_dest=logs_dir / f"{pod_name}.log",
        )

    return f"Captured {len(pod_names)} pod(s) and their logs"


@task
def delete_resources(args, ctx):
    """Delete the Praxis Deployment (owned resources are garbage-collected)."""
    best_effort_oc(
        "delete",
        "deployment",
        args.deployment_name,
        "-n",
        args.namespace,
        "--ignore-not-found",
    )
    return "Deleted Praxis Deployment"


if __name__ == "__main__":
    run.main()
