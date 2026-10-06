#!/usr/bin/env python3

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger("TOOLBOX")


@entrypoint
def run(*, namespace: str, lws_name: str, service_name: str, pod_selector: str):
    """Capture the LeaderWorkerSet deployment, Service, pods, events, and logs."""
    return execute_tasks(locals())


@task
def create_artifact_directory(args, ctx):
    ctx.artifact_directory = args.artifact_dir / "artifacts"
    ctx.artifact_directory.mkdir(parents=True, exist_ok=True)
    return f"Created {ctx.artifact_directory}"


@task
def capture_leader_worker_set(args, ctx):
    return _capture(
        "LeaderWorkerSet",
        ctx.artifact_directory / "leaderworkerset.yaml",
        "get",
        "leaderworkerset",
        args.lws_name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
    )


@task
def capture_service(args, ctx):
    return _capture(
        "Service",
        ctx.artifact_directory / "service.yaml",
        "get",
        "service",
        args.service_name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
    )


@task
def capture_pods(args, ctx):
    return _capture(
        "LeaderWorkerSet pods",
        ctx.artifact_directory / "leaderworkerset.pods.yaml",
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        args.pod_selector,
        "-o",
        "yaml",
    )


@task
def capture_resource_descriptions(args, ctx):
    captures = (
        (
            "LeaderWorkerSet description and events",
            ctx.artifact_directory / "leaderworkerset.describe.txt",
            "describe",
            "leaderworkerset",
            args.lws_name,
            "-n",
            args.namespace,
        ),
        (
            "Service description and events",
            ctx.artifact_directory / "service.describe.txt",
            "describe",
            "service",
            args.service_name,
            "-n",
            args.namespace,
        ),
        (
            "pod descriptions and events",
            ctx.artifact_directory / "leaderworkerset.pods.describe.txt",
            "describe",
            "pods",
            "-n",
            args.namespace,
            "-l",
            args.pod_selector,
        ),
    )
    return "\n".join(_capture(*capture) for capture in captures)


@task
def capture_pod_logs(args, ctx):
    return _capture(
        "LeaderWorkerSet pod logs",
        ctx.artifact_directory / "leaderworkerset.pods.log",
        "logs",
        "-n",
        args.namespace,
        "-l",
        args.pod_selector,
        "--all-containers=true",
        "--prefix=true",
    )


def _capture(description: str, path, *command: str) -> str:
    try:
        result = oc(
            *command,
            check=False,
            log_stdout=False,
            log_stderr=False,
            stdout_dest=path,
        )
    except Exception as exc:
        logger.warning("Failed to capture %s: %s", description, exc)
        return f"Failed to capture {description}"
    if result.returncode:
        logger.warning(
            "Failed to capture %s (oc exited with status %d)", description, result.returncode
        )
        return f"Failed to capture {description}"
    return f"Captured {description} to {path}"
