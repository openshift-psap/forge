#!/usr/bin/env python3

from __future__ import annotations

import logging

from projects.core.dsl import always, entrypoint, execute_tasks, retry, shell, task, template
from projects.core.dsl.utils.k8s import oc_resource_exists

LWS_OPERATOR_NAME = "cluster"

logger = logging.getLogger(__name__)


@entrypoint
def run(*, timeout_seconds: int = 600) -> int:
    """
    Bootstrap the LeaderWorkerSetOperator instance and wait for it to be ready.

    Args:
        timeout_seconds: Maximum time to wait for the LWS operator configuration
    """

    execute_tasks(locals())
    return 0


@task
def setup_directories(args, ctx):
    """Create command source and artifact directories"""

    shell.mkdir("src")
    shell.mkdir("artifacts")
    return "Prepared LWS operator bootstrap command"


@task
def render_manifest(args, ctx):
    """Render the LeaderWorkerSetOperator manifest"""

    ctx.manifest_file = args.artifact_dir / "src" / "leaderworkersetoperator.yaml"
    template.render_template_to_file("leaderworkersetoperator.yaml.j2", ctx.manifest_file)
    return "Rendered LeaderWorkerSetOperator manifest"


@task
def apply_manifest_if_missing(args, ctx):
    """Apply the LeaderWorkerSetOperator manifest when missing"""

    if oc_resource_exists("leaderworkersetoperator", LWS_OPERATOR_NAME):
        return f"LeaderWorkerSetOperator/{LWS_OPERATOR_NAME} already exists"

    shell.run(f"oc apply -f {ctx.manifest_file}")
    return f"Applied LeaderWorkerSetOperator/{LWS_OPERATOR_NAME}"


@retry(attempts=60, delay=10)
@task
def wait_for_lws_operator_resource(args, ctx):
    """Wait for the LeaderWorkerSetOperator resource to exist"""

    if oc_resource_exists("leaderworkersetoperator", LWS_OPERATOR_NAME):
        return f"LeaderWorkerSetOperator/{LWS_OPERATOR_NAME} exists"
    return False


@retry(attempts=30, delay=10)
@task
def wait_for_lws_operator_ready(args, ctx):
    """Wait for LWS operator to be ready"""

    result = shell.run(
        f"oc get leaderworkersetoperator {LWS_OPERATOR_NAME} "
        "-o jsonpath='{.status.conditions[?(@.type==\"Available\")].status}'",
        check=False,
        log_stdout=False,
    )

    if not result.success:
        return (False, f"Failed to get LeaderWorkerSetOperator/{LWS_OPERATOR_NAME} status")

    available_status = result.stdout.strip().strip("'")
    if available_status == "True":
        return f"LeaderWorkerSetOperator/{LWS_OPERATOR_NAME} is available"

    return (
        False,
        f"LeaderWorkerSetOperator/{LWS_OPERATOR_NAME} not yet available (status: {available_status})",
    )


@always
@task
def capture_lws_operator_state(args, ctx):
    """Capture the LeaderWorkerSetOperator resource state"""

    shell.run(
        f"oc get leaderworkersetoperator {LWS_OPERATOR_NAME} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "leaderworkersetoperator.yaml",
        check=False,
    )
    shell.run(
        "oc get leaderworkersetoperator -o wide",
        stdout_dest=args.artifact_dir / "artifacts" / "leaderworkersetoperators.status",
        check=False,
    )
    return "Captured LWS operator bootstrap state"


if __name__ == "__main__":
    run.main()
