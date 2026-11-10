#!/usr/bin/env python3

"""
Deploy Trace Collector Toolbox

Creates the per-capture OpenTelemetry Collector that receives OTLP traces from
instrumented serving components and spools them as newline-delimited OTLP JSON
on a dedicated PVC. Everything is owned by a single capture ID so cleanup can
never touch another job's resources.

Can be run standalone:
    ./bin/run_toolbox cluster deploy_trace_collector \\
        --namespace my-ns --capture-id cap-123 --job-id job-7 \\
        --project llm_d --deployment-id llmd-abc
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from projects.cluster.library.tracing.collection import (
    build_collector_resources,
    collector_service_name,
    load_collector_config,
)
from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    TracingSettings,
)
from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc_apply, oc_get_json

logger = logging.getLogger("DSL")

TEMPLATES_DIR = Path(__file__).parent / "templates"


@entrypoint
def run(
    namespace: str,
    capture_id: str,
    job_id: str,
    project: str,
    deployment_id: str,
    *,
    mode: str = "standard",
    sample_ratio: float = 0.1,
    collector_image: str | None = None,
    storage_size: str = "10Gi",
    storage_class: str | None = None,
    ready_timeout_seconds: int = 300,
    manifest_dir: str | Path | None = None,
) -> int:
    """
    Deploy the FORGE OpenTelemetry Collector for one capture.

    Args:
        namespace: Namespace for the collector resources
        capture_id: Stable deployment-capture identity (unique per capture)
        job_id: FORGE job / local invocation identity
        project: Owning FORGE project name (e.g. llm_d, rhaiis)
        deployment_id: Stable serving deployment identity
        mode: Tracing mode (off, standard, detailed)
        sample_ratio: Parent-based trace-ID-ratio sampling value in [0, 1]
        collector_image: Collector image override (defaults to the pinned image)
        storage_size: PVC storage request for the spool
        storage_class: Optional StorageClass for the spool PVC
        ready_timeout_seconds: Bounded readiness wait budget for the collector
        manifest_dir: Directory to record applied manifests (defaults to ARTIFACT_DIR/src)
    """
    execute_tasks(locals())
    return 0


@task
def validate_inputs(args, ctx):
    """Validate the typed inputs and resolve tracing settings."""
    from projects.cluster.library.tracing.models import parse_mode, parse_sample_ratio

    ctx.mode = parse_mode(args.mode)
    ctx.sample_ratio = parse_sample_ratio(args.sample_ratio)

    if abs(ctx.sample_ratio - float(args.sample_ratio)) > 1e-9:
        raise ValueError("sample_ratio must be a number between 0 and 1")

    if not ctx.mode.enabled:
        raise ValueError("tracing.mode is 'off'; deploy_trace_collector must not be invoked")

    ctx.identity = CaptureIdentity(
        job_id=args.job_id,
        capture_id=args.capture_id,
        project=args.project,
        namespace=args.namespace,
        deployment_id=args.deployment_id,
    )

    settings_kwargs = {
        "mode": ctx.mode,
        "sample_ratio": ctx.sample_ratio,
        "storage_size": args.storage_size,
        "storage_class": args.storage_class,
        "ready_timeout_seconds": args.ready_timeout_seconds,
    }
    if args.collector_image:
        settings_kwargs["collector_image"] = args.collector_image
    ctx.settings = TracingSettings(**settings_kwargs)

    ctx.manifest_dir = Path(args.manifest_dir) if args.manifest_dir else args.artifact_dir / "src"
    ctx.manifest_dir.mkdir(parents=True, exist_ok=True)
    ctx.ready_deadline = time.monotonic() + ctx.settings.ready_timeout_seconds

    return f"Deploying collector for capture {args.capture_id} ({ctx.mode.value})"


@task
def apply_resources(args, ctx):
    """Apply the collector ConfigMap, PVC, Deployment, and Service.

    Only non-secret resources are applied, always through ``oc_apply`` (which
    records the manifest in the artifact directory).
    """
    config_text = load_collector_config(TEMPLATES_DIR)
    resources = build_collector_resources(
        identity=ctx.identity,
        settings=ctx.settings,
        collector_config_text=config_text,
    )

    for resource in resources:
        kind = resource["kind"].lower()
        name = resource["metadata"]["name"]
        path = ctx.manifest_dir / f"{kind}-{name}.yaml"
        oc_apply(path, resource)

    ctx.resource_names = [r["metadata"]["name"] for r in resources]
    return f"Applied {len(resources)} collector resource(s)"


# Upper bound on readiness polls; the configured ready_timeout_seconds deadline
# is the real limit, so a larger configured budget is not cut short.
READY_MAX_ATTEMPTS = 10_000


@retry(attempts=READY_MAX_ATTEMPTS, delay=5, backoff=1.0)
@task
def wait_for_ready(args, ctx):
    """Poll the collector Deployment until it reports an available replica.

    Bounded by ``ready_timeout_seconds``: when the deadline passes the wait
    fails instead of blocking for a fixed number of attempts.
    """
    deadline = getattr(ctx, "ready_deadline", None)
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError(
            f"collector did not become ready within {ctx.settings.ready_timeout_seconds}s"
        )

    deployment = oc_get_json(
        "deployment",
        name=collector_service_name(ctx.identity.capture_id),
        namespace=ctx.identity.namespace,
        ignore_not_found=True,
    )
    if not deployment:
        return (False, "collector deployment not found yet")
    available = deployment.get("status", {}).get("availableReplicas", 0)
    if available and available >= 1:
        return f"Collector ready ({available} replica(s))"
    return (False, "collector has no available replica yet")


if __name__ == "__main__":
    run.main()
