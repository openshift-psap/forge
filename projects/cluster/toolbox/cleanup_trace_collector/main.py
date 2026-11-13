#!/usr/bin/env python3

"""
Cleanup Trace Collector Toolbox

Removes the collector Deployment, Service, ConfigMap, PVC, and spool for a
single capture. Cleanup is restricted to the capture's ownership label, so it
can never delete another job's resources.

Can be run standalone:
    ./bin/run_toolbox cluster cleanup_trace_collector \\
        --namespace my-ns --capture-id cap-123
"""

from __future__ import annotations

import logging

from projects.cluster.library.tracing.collection import cleanup_selector
from projects.cluster.library.tracing.models import CaptureIdentity
from projects.core.dsl import always, entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger("DSL")


@entrypoint
def run(
    namespace: str,
    capture_id: str,
    *,
    job_id: str = "",
    project: str = "",
    deployment_id: str = "",
) -> int:
    """
    Remove a capture's collector resources.

    Args:
        namespace: Namespace holding the collector resources
        capture_id: Capture identity whose resources should be removed
        job_id: FORGE job / local invocation identity (for labels)
        project: Owning FORGE project name (for labels)
        deployment_id: Stable serving deployment identity (for labels)
    """
    execute_tasks(locals())
    return 0


@task
def validate_inputs(args, ctx):
    """Validate the capture identity."""
    ctx.identity = CaptureIdentity(
        job_id=args.job_id or "unknown",
        capture_id=args.capture_id,
        project=args.project or "unknown",
        namespace=args.namespace,
        deployment_id=args.deployment_id or "unknown",
    )
    return f"Cleaning up capture {ctx.identity.capture_id}"


@task
def delete_resources(args, ctx):
    """Delete resources owned by this capture via its ownership label."""
    selector = cleanup_selector(ctx.identity)
    oc(
        "-n",
        ctx.identity.namespace,
        "delete",
        "deployment,service,configmap,pvc",
        "-l",
        selector,
        "--ignore-not-found=true",
    )
    return f"Deleted resources matching {selector}"


@always
@task
def confirm_removed(args, ctx):
    """Confirm no resources remain for this capture."""
    selector = cleanup_selector(ctx.identity)
    result = oc(
        "-n",
        ctx.identity.namespace,
        "get",
        "deployment,service,configmap,pvc",
        "-l",
        selector,
        "-o",
        "name",
        check=False,
    )
    remaining = [line for line in result.stdout.splitlines() if line.strip()]
    if remaining:
        logger.warning("Resources still present for %s: %s", selector, ", ".join(remaining))
        return f"{len(remaining)} resource(s) still present"
    return "All capture resources removed"


if __name__ == "__main__":
    run.main()
