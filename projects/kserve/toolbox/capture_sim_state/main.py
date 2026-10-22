from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger("TOOLBOX")


@entrypoint
def run(
    *,
    namespace: str,
    name: str = "sim-llm",
):
    """Capture artifacts for a simulator Deployment.

    Collects the Deployment YAML, Deployment description, Pod YAML, Pod events,
    and container logs for the simulator.

    Args:
        namespace: Namespace of the sim Deployment.
        name: Name of the sim Deployment.
    """

    if not name:
        raise ValueError("name must not be empty")

    return execute_tasks(locals())


SELECTOR = "app={name}"


@task
def capture_deployment_yaml(args, ctx):
    """Capture the simulator Deployment definition."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "get",
        "deployment",
        args.name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
        check=False,
        stdout_dest=artifacts_dir / "deployment.yaml",
    )

    return f"Deployment {args.name} YAML captured"


@task
def capture_deployment_description(args, ctx):
    """Capture the simulator Deployment description with events."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "describe",
        "deployment",
        args.name,
        "-n",
        args.namespace,
        check=False,
        stdout_dest=artifacts_dir / "deployment_description.txt",
    )

    return f"Deployment {args.name} description captured"


@task
def capture_pods_yaml(args, ctx):
    """Capture YAML for all Pods of the sim Deployment."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    selector = SELECTOR.format(name=args.name)
    oc(
        "get",
        "pods",
        "-l",
        selector,
        "-n",
        args.namespace,
        "-o",
        "yaml",
        check=False,
        stdout_dest=artifacts_dir / "pods.yaml",
    )

    return f"Pod YAML for {selector} captured"


@task
def capture_pod_events(args, ctx):
    """Capture events for all Pods of the sim Deployment."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    selector = SELECTOR.format(name=args.name)
    oc(
        "describe",
        "pods",
        "-l",
        selector,
        "-n",
        args.namespace,
        check=False,
        stdout_dest=artifacts_dir / "pod_descriptions.txt",
    )

    return f"Pod events for {selector} captured"


@task
def capture_pod_logs(args, ctx):
    """Capture logs from all containers of the sim Deployment Pods."""
    logs_dir = args.artifact_dir / "artifacts" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    selector = SELECTOR.format(name=args.name)
    result = oc(
        "get",
        "pods",
        "-l",
        selector,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.items[*].metadata.name}",
        check=False,
        log_stdout=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return "No pods found to capture logs"

    captured = 0
    for pod_name in pod_names:
        oc(
            "logs",
            pod_name,
            "-n",
            args.namespace,
            "--all-containers",
            "--tail=500",
            check=False,
            stdout_dest=logs_dir / f"{pod_name}.log",
        )
        captured += 1

    return f"Logs captured for {captured} pods"


if __name__ == "__main__":
    run.main()
