#!/usr/bin/env python3

"""
Capture Prometheus TSDB Toolbox

Extracts cluster Prometheus metrics for a specific time window and saves them
as a compressed OpenMetrics archive. The output can later be imported into a
local Prometheus instance for offline querying.

Can be run standalone:
    ./bin/run_toolbox cluster capture_prometheus \\
        "2026-07-26T10:00:00+00:00" \\
        "2026-07-26T10:20:00+00:00" \\
        /path/to/output
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from projects.core.dsl import always, entrypoint, execute_tasks, shell, task
from projects.core.dsl.utils.k8s import oc, oc_exec

logger = logging.getLogger("DSL")

PROMETHEUS_NAMESPACE = "openshift-monitoring"
PROMETHEUS_POD = "prometheus-k8s-0"
PROMETHEUS_CONTAINER = "prometheus"
TSDB_PATH = "/prometheus"


@entrypoint
def run(
    start_time: datetime,
    end_time: datetime,
    output_dir: str | Path | None = None,
    *,
    pod_name: str = PROMETHEUS_POD,
    namespace: str = PROMETHEUS_NAMESPACE,
    container: str = PROMETHEUS_CONTAINER,
    head_only: bool = True,
) -> int:
    """
    Capture Prometheus metrics for a specific time window.

    Extracts all metrics between start_time and end_time from the cluster's
    Prometheus TSDB and writes them as a compressed OpenMetrics file to output_dir.

    Args:
        start_time: Start of the capture window (UTC)
        end_time: End of the capture window (UTC)
        output_dir: Directory to write the metrics archive into (optional, defaults to artifacts_dir/prom_db)
        pod_name: Prometheus pod to extract from
        namespace: Namespace where Prometheus runs
        container: Container name within the Prometheus pod
        head_only: if true, restrict the TSDB scan to the recent in-memory data
    """
    execute_tasks(locals())
    return 0


def _pod_kwargs(ctx) -> dict:
    """Return the common namespace/pod/container kwargs for oc_exec/oc_cp_from_pod."""
    return {"namespace": ctx.namespace, "pod": ctx.pod_name, "container": ctx.container}


def _parse_time(value) -> datetime:
    """Parse a datetime from string (ISO format) or pass through if already datetime.

    Naive datetimes are assumed UTC. Aware datetimes are normalized to UTC.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@task
def validate_parameters(args, ctx):
    """Validate inputs and compute time bounds."""

    ctx.start_time = _parse_time(args.start_time)
    ctx.end_time = _parse_time(args.end_time)

    if ctx.end_time <= ctx.start_time:
        raise ValueError("end_time must be after start_time")

    max_duration_seconds = 2 * 3600
    duration = (ctx.end_time - ctx.start_time).total_seconds()
    if duration > max_duration_seconds:
        raise ValueError(
            f"Test duration ({int(duration)}s) exceeds the 2-hour WAL window. "
            f"Tests longer than 2 hours require persistent block handling (not yet implemented)."
        )

    if args.output_dir is not None:
        ctx.output_dir = Path(args.output_dir)
    else:
        ctx.output_dir = args.artifact_dir / "prom_db"
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    ctx.start_ms = int(ctx.start_time.timestamp() * 1000)
    ctx.end_ms = int(ctx.end_time.timestamp() * 1000)
    ctx.duration_seconds = int((ctx.end_time - ctx.start_time).total_seconds())

    ctx.namespace = args.namespace
    ctx.pod_name = args.pod_name
    ctx.container = args.container

    return (
        f"Capture window: {ctx.start_time.isoformat()} → {ctx.end_time.isoformat()} "
        f"({ctx.duration_seconds}s)"
    )


@task
def validate_prometheus_pod(args, ctx):
    """Verify the Prometheus pod is running and accessible."""

    result = oc(
        "-n",
        ctx.namespace,
        "get",
        "pod",
        ctx.pod_name,
        "-o",
        "jsonpath={.status.phase}",
    )

    phase = result.stdout.strip()
    if phase != "Running":
        raise RuntimeError(
            f"Prometheus pod {ctx.pod_name} is in phase '{phase}', expected 'Running'"
        )

    return f"Prometheus pod {ctx.pod_name} is running"


@task
def create_temp_tsdb_dir(args, ctx):
    """Create a temp directory on the PVC with symlinks to WAL and chunks_head only.

    This limits promtool to scanning only recent in-memory data (~1-2 GB)
    instead of the full TSDB which can be tens of GB.
    """

    if not args.head_only:
        logger.info("head_only isn't set, skipping.")
        return

    ctx.temp_dir = f"{TSDB_PATH}/.capture-tmp-{ctx.start_ms}"

    oc_exec(
        "sh",
        "-c",
        f"mkdir -p {ctx.temp_dir}"
        f" && ln -sf {TSDB_PATH}/wal {ctx.temp_dir}/wal"
        f" && ln -sf {TSDB_PATH}/chunks_head {ctx.temp_dir}/chunks_head",
        **_pod_kwargs(ctx),
    )

    return f"Created temp TSDB dir at {ctx.temp_dir}"


@task
def dump_metrics(args, ctx):
    """Run promtool tsdb dump-openmetrics with time filtering against the temp dir."""

    output_file = ctx.output_dir / "open_metrics.gz"
    oc_exec(
        "sh",
        "-c",
        f"set -o pipefail && promtool tsdb dump-openmetrics"
        f" --min-time={ctx.start_ms} --max-time={ctx.end_ms}"
        f" {ctx.temp_dir if args.head_only else TSDB_PATH}"
        f" | gzip",
        stdout_dest=output_file,
        text=False,
        **_pod_kwargs(ctx),
    )

    size_result = shell.run(
        ["stat", "-c", "%s", str(output_file)], shell=False, check=False, capture_output=True
    )

    ctx.archive_size_bytes = int(size_result.stdout.strip()) if size_result.success else 0

    return f"Dumped metrics ({ctx.archive_size_bytes} bytes compressed)"


@always
@task
def cleanup_pod(args, ctx):
    """Remove temp files from the Prometheus pod."""

    temp_dir = getattr(ctx, "temp_dir", None)
    kwargs = _pod_kwargs(ctx)

    for path in (temp_dir,):
        if path:
            oc_exec("rm", "-rf", path, check=False, **kwargs)

    return "Cleaned up temp files on Prometheus pod"


if __name__ == "__main__":
    run.main()
