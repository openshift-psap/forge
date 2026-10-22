#!/usr/bin/env python3

from __future__ import annotations

import logging

from projects.core.dsl import (
    always,
    entrypoint,
    execute_tasks,
    retry,
    task,
)
from projects.core.dsl.utils.k8s import condition_status, oc, oc_get_json

logger = logging.getLogger(__name__)

SELECTOR = "serving.kserve.io/inferenceservice={name}"


@entrypoint
def run(
    *,
    name: str,
    namespace: str,
    timeout_seconds: int = 3600,
    health_check_timeout: int = 120,
    poll_interval: int = 10,
):
    """
    Wait for a KServe InferenceService to become ready.

    Args:
        name: Name of the InferenceService
        namespace: Namespace of the InferenceService
        timeout_seconds: Maximum time to wait for readiness
        health_check_timeout: Maximum time for the health check after readiness
        poll_interval: Seconds between poll attempts
    """
    return execute_tasks(locals())


@task
def setup(args, ctx):
    ctx.selector = SELECTOR.format(name=args.name)
    ctx.max_attempts = max(1, args.timeout_seconds // args.poll_interval)
    ctx.health_attempts = max(1, args.health_check_timeout // args.poll_interval)
    return (
        f"Will poll every {args.poll_interval}s, "
        f"max {ctx.max_attempts} attempts for readiness, "
        f"{ctx.health_attempts} attempts for health"
    )


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_pods_appear(args, ctx):
    """Wait for InferenceService pods to appear"""

    result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        ctx.selector,
        "--no-headers",
        check=False,
        log_stdout=True,
    )

    if (
        result.returncode == 0
        and result.stdout.strip()
        and "No resources found" not in result.stdout
    ):
        return f"Pods appeared for {args.name}"

    return False, "Pods not found yet"


@retry(attempts=10, delay=6, backoff=1.0)
@task
def wait_pods_scheduled(args, ctx):
    """Wait for all pods to be scheduled, abort on image pull errors"""

    result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        ctx.selector,
        "--no-headers",
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        return False, "No pods found yet"

    image_pull_result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        ctx.selector,
        "--no-headers",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}:{range .status.containerStatuses[*]}{.state.waiting.reason}{'|'}{end}{'\\n'}{end}",
        check=False,
    )

    for line in image_pull_result.stdout.strip().split("\n"):
        if ":" not in line:
            continue
        pod_name, waiting_reasons = line.split(":", 1)
        for reason in waiting_reasons.split("|"):
            if reason in ("ImagePullBackOff", "ErrImagePull"):
                raise RuntimeError(
                    f"Pod {pod_name} has image pull error: {reason}. "
                    "Aborting wait due to image pull failure."
                )

    if "Pending" in result.stdout or "SchedulingGated" in result.stdout:
        return False, "Waiting for pods to be scheduled"

    return f"All pods for {args.name} are scheduled"


@retry(attempts=360, delay=10, backoff=1.0)
@task
def wait_for_ready(args, ctx):
    """Wait for InferenceService Ready condition"""

    isvc = oc_get_json(
        "inferenceservice",
        name=args.name,
        namespace=args.namespace,
        ignore_not_found=True,
    )
    if isvc is None:
        return False, f"InferenceService {args.name} not found yet"

    oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        ctx.selector,
        log_stdout=True,
    )

    _check_pod_restarts(args, ctx)

    ready = condition_status(isvc, "Ready")
    if ready == "True":
        return f"InferenceService {args.name} is Ready"

    conditions = isvc.get("status", {}).get("conditions", [])
    reasons = [f"{c['type']}={c.get('status', '?')}({c.get('reason', '')})" for c in conditions]
    return False, f"InferenceService {args.name} not ready: {', '.join(reasons)}"


def _check_pod_restarts(args, ctx):
    """Abort if any pod has restarted"""

    restart_result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        ctx.selector,
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}:{.status.initContainerStatuses[*].restartCount} {.status.containerStatuses[*].restartCount}{'\\n'}{end}",
    )

    for line in restart_result.stdout.strip().split("\n"):
        if ":" not in line:
            continue
        pod_name, restart_counts = line.split(":", 1)
        for count_str in restart_counts.split():
            try:
                if int(count_str) > 0:
                    raise RuntimeError(
                        f"Pod {pod_name} has restarted (restart count: {count_str}). "
                        "Aborting wait due to pod restart."
                    )
            except ValueError:
                pass


@task
def verify_health(args, ctx):
    """Run a health check on the predictor pod"""

    pod_name_result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        ctx.selector,
        "-o",
        "jsonpath={.items[0].metadata.name}",
        check=False,
        log_stdout=False,
    )
    if pod_name_result.returncode != 0 or not pod_name_result.stdout.strip():
        return "No pod found — skipping health check (InferenceService is Ready)"

    pod_name = pod_name_result.stdout.strip()
    health_result = oc(
        "exec",
        pod_name,
        "-n",
        args.namespace,
        "-c",
        "kserve-container",
        "--",
        "curl",
        "-sf",
        "http://localhost:8080/health",
        check=False,
        log_stdout=False,
    )
    if health_result.returncode != 0:
        return (
            f"Health endpoint not responding on {pod_name} (InferenceService is Ready, proceeding)"
        )

    return f"Health check passed on pod {pod_name}"


@always
@task
def capture_isvc_description(args, ctx):
    """Capture InferenceService description with events"""

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "describe",
        "inferenceservice",
        args.name,
        "-n",
        args.namespace,
        check=False,
        stdout_dest=artifacts_dir / "inferenceservice.describe.txt",
    )

    return "Captured InferenceService description"


@always
@task
def capture_isvc_yaml(args, ctx):
    """Capture final InferenceService YAML"""

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "get",
        "inferenceservice",
        args.name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
        check=False,
        stdout_dest=artifacts_dir / "inferenceservice.yaml",
    )

    return "Captured InferenceService YAML"


@always
@task
def capture_workload_overview(args, ctx):
    """Capture deployment, replicaset, and pod overview"""

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    selector = getattr(ctx, "selector", None)
    if not selector:
        return "No selector available"

    oc(
        "get",
        "deploy,rs,pod",
        "-l",
        selector,
        "-n",
        args.namespace,
        "-o",
        "wide",
        check=False,
        stdout_dest=artifacts_dir / "workload_overview.txt",
    )

    return "Captured workload overview"


@always
@task
def capture_pod_descriptions(args, ctx):
    """Capture pod descriptions for debugging"""

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    selector = getattr(ctx, "selector", None)
    if not selector:
        return "No selector available"

    pod_result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        selector,
        "-o",
        "jsonpath={.items[*].metadata.name}",
        log_stdout=False,
        check=False,
    )

    pod_names = pod_result.stdout.strip().split()
    if not pod_names or not pod_result.stdout.strip():
        return "No pods found to describe"

    pod_desc_path = artifacts_dir / "pod_descriptions.txt"
    with open(pod_desc_path, "w", encoding="utf-8") as f:
        for pod_name in pod_names:
            describe_result = oc(
                "describe",
                "pod",
                pod_name,
                "-n",
                args.namespace,
                log_stdout=False,
                check=False,
            )
            f.write(f"=== Description for pod: {pod_name} ===\n")
            f.write(describe_result.stdout)
            f.write("\n\n")

    return f"Captured descriptions for {len(pod_names)} pods"


@always
@task
def capture_pod_logs(args, ctx):
    """Capture pod logs for debugging"""

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    selector = getattr(ctx, "selector", None)
    if not selector:
        return "No selector available"

    pod_result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        selector,
        "-o",
        "jsonpath={.items[*].metadata.name}",
        log_stdout=False,
        check=False,
    )

    pod_names = pod_result.stdout.strip().split()
    if not pod_names or not pod_result.stdout.strip():
        return "No pods found to capture logs"

    log_path = artifacts_dir / "pod_logs.txt"
    with open(log_path, "w", encoding="utf-8") as f:
        for pod_name in pod_names:
            log_result = oc(
                "logs",
                pod_name,
                "-n",
                args.namespace,
                "--all-containers=true",
                log_stdout=False,
                check=False,
            )
            f.write(f"=== Logs for pod: {pod_name} ===\n")
            f.write(log_result.stdout)
            f.write("\n\n")

    return f"Captured logs for {len(pod_names)} pods"


@always
@task
def capture_replicaset_description(args, ctx):
    """Capture ReplicaSet descriptions for pod creation failure analysis"""

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    selector = getattr(ctx, "selector", None)
    if not selector:
        return "No selector available"

    rs_result = oc(
        "get",
        "replicaset",
        "-l",
        selector,
        "-n",
        args.namespace,
        "-o",
        "name",
        log_stdout=False,
        check=False,
    )

    rs_names = [n.strip() for n in rs_result.stdout.strip().split("\n") if n.strip()]
    if not rs_names:
        return "No replicasets found"

    rs_desc_path = artifacts_dir / "replicaset_description.txt"
    with open(rs_desc_path, "w", encoding="utf-8") as f:
        for rs_name in rs_names:
            desc_result = oc(
                "describe",
                rs_name,
                "-n",
                args.namespace,
                log_stdout=False,
                check=False,
            )
            f.write(desc_result.stdout)
            f.write("\n\n")

    return f"Captured descriptions for {len(rs_names)} replicasets"


if __name__ == "__main__":
    run.main()
