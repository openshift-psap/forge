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
from datetime import UTC, datetime
from pathlib import Path

from projects.cluster.library.tracing.collection import (
    build_collector_resources,
    cleanup_selector,
    collector_service_name,
    load_collector_config,
)
from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    TracingSettings,
)
from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_apply, oc_get_json

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
    ctx.started_at = datetime.now(UTC).isoformat()
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


@task
def verify_storage(args, ctx):
    """Verify the spool PVC bound and that the writable probe succeeded.

    The Deployment's ``spool-probe`` init container writes and removes a probe
    with the same volume permissions the collector uses. The artifact reader
    sidecar stays read-only, so it is never used to test writability.
    """
    ctx.collector_pod = _collector_pod(ctx)
    pod = oc_get_json(
        "pod",
        name=ctx.collector_pod,
        namespace=ctx.identity.namespace,
    )
    init_statuses = pod.get("status", {}).get("initContainerStatuses", []) or []
    probe = next((s for s in init_statuses if s.get("name") == "spool-probe"), None)
    if not probe or probe.get("state", {}).get("terminated", {}).get("exitCode") != 0:
        raise RuntimeError("spool write probe did not succeed; PVC is not writable")

    return "Spool PVC is Bound and writable"


def _collector_pod(ctx) -> str:
    from projects.cluster.library.tracing.collection import cleanup_selector

    selector = cleanup_selector(ctx.identity)
    result = oc(
        "-n",
        ctx.identity.namespace,
        "get",
        "pods",
        "-l",
        selector,
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    pod = result.stdout.strip()
    if not pod:
        raise RuntimeError("no collector pod found for this capture")
    return pod


@always
@task
def write_capture_state(args, ctx):
    """Persist capture identity and collector readiness for later finalization.

    This file lets a finalization phase recover the capture identity even after
    an interruption, since ``finally`` blocks cannot run after SIGKILL. It runs
    after ``validate_inputs`` (``@always``), so when validation failed the
    identity is absent and there is nothing to recover; return without masking
    the original error.
    """
    if getattr(ctx, "identity", None) is None:
        return "No capture identity (input validation failed); nothing to persist"
    state = {
        "capture_id": ctx.identity.capture_id,
        "job_id": ctx.identity.job_id,
        "project": ctx.identity.project,
        "namespace": ctx.identity.namespace,
        "deployment_id": ctx.identity.deployment_id,
        "mode": ctx.mode.value,
        "sample_ratio": ctx.sample_ratio,
        "collector_pod": getattr(ctx, "collector_pod", ""),
        "started_at": ctx.started_at,
        "ownership_selector": cleanup_selector(ctx.identity),
    }
    path = ctx.manifest_dir.parent / "trace-capture-state.json"
    from projects.core.dsl.utils import write_json

    write_json(path, state)
    return f"Wrote capture state to {path}"


if __name__ == "__main__":
    run.main()
