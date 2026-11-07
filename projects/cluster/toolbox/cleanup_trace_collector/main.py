#!/usr/bin/env python3

"""
Cleanup Trace Collector Toolbox

Removes the collector Deployment, Service, ConfigMap, PVC, and spool for a
single capture. Cleanup is restricted to the capture's ownership label, so it
can never delete another job's resources.

By default cleanup refuses to run when the capture has no verified bundle, to
avoid destroying the only remaining trace copy. Pass ``--force`` to remove
resources regardless (for an explicit operator-driven cleanup).

Can be run standalone:
    ./bin/run_toolbox cluster cleanup_trace_collector \\
        --namespace my-ns --capture-id cap-123
"""

from __future__ import annotations

import logging
from pathlib import Path

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
    capture_result_file: str | Path | None = None,
    force: bool = False,
) -> int:
    """
    Remove a capture's collector resources.

    Args:
        namespace: Namespace holding the collector resources
        capture_id: Capture identity whose resources should be removed
        job_id: FORGE job / local invocation identity (for labels)
        project: Owning FORGE project name (for labels)
        deployment_id: Stable serving deployment identity (for labels)
        capture_result_file: Capture result JSON; when present and successful, cleanup is safe
        force: Remove resources even when no verified capture result is available
    """
    execute_tasks(locals())
    return 0


@task
def validate_inputs(args, ctx):
    """Validate the capture identity and the safe-to-clean condition."""
    ctx.identity = CaptureIdentity(
        job_id=args.job_id or "unknown",
        capture_id=args.capture_id,
        project=args.project or "unknown",
        namespace=args.namespace,
        deployment_id=args.deployment_id or "unknown",
    )
    ctx.force = bool(args.force)
    ctx.capture_result_file = Path(args.capture_result_file) if args.capture_result_file else None

    if ctx.force:
        return "Force cleanup requested"

    if ctx.capture_result_file is None:
        raise RuntimeError(
            "refusing to clean up without a verified capture result; pass "
            "--capture-result-file or --force"
        )
    if not ctx.capture_result_file.is_file():
        raise FileNotFoundError(f"capture result not found: {ctx.capture_result_file}")

    import json

    result = json.loads(ctx.capture_result_file.read_text(encoding="utf-8"))
    if result.get("status") not in ("collected", "empty"):
        raise RuntimeError(
            f"capture result status is '{result.get('status')}', not verified; "
            "pass --force to remove resources anyway"
        )
    return "Capture result verified; safe to clean up"


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
