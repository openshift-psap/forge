#!/usr/bin/env python3

"""
Capture Prometheus Metrics via In-Cluster Queries

Runs PromQL range queries from inside the cluster by creating a temporary
curl pod in openshift-monitoring. Queries are read from a YAML file
(name: promql dict) and results are saved as raw JSON files.

Can be run standalone:
    ./bin/run_toolbox cluster capture_prometheus_metrics \\
        /path/to/queries.yaml \\
        "2026-07-26T10:00:00+00:00" \\
        "2026-07-26T10:20:00+00:00" \\
        /path/to/output
"""

from __future__ import annotations

import json
import logging
import shlex  # noqa: F401 - used in execute_queries
from datetime import UTC, datetime
from pathlib import Path

import yaml

from projects.core.dsl import always, entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc, oc, oc_exec

logger = logging.getLogger("DSL")

CURL_IMAGE = "quay.io/curl/curl:8.21.0"
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

PROM_NAMESPACE = "openshift-monitoring"
PROM_URL = "https://thanos-querier.openshift-monitoring.svc:9091"
PROM_SERVICE_ACCOUNT = "prometheus-k8s"


@entrypoint
def run(
    queries_file: str,
    start_time: datetime,
    end_time: datetime,
    output_dir: str | Path | None = None,
    *,
    step_seconds: int = 15,
    prom_namespace: str | None = None,
    prom_url: str | None = None,
    prom_service_account: str | None = None,
) -> int:
    """
    Query Prometheus from inside the cluster and save raw results as JSON.

    Creates a temporary curl pod, executes each PromQL range query against
    the Prometheus/Thanos service, and writes the raw JSON response to the
    output directory.

    Args:
        queries_file: Path to a YAML file with a name:promql dict.
        start_time: Start of the query window (UTC).
        end_time: End of the query window (UTC).
        step_seconds: Query resolution step in seconds.
        output_dir: Directory to write results into (default: artifact_dir/prometheus_metrics).
        prom_namespace: Override the namespace for the curl pod.
        prom_url: Override the Prometheus/Thanos URL.
        prom_service_account: Override the service account for the curl pod.
    """
    ctx = execute_tasks(locals())
    return ctx.output_dir


def _parse_time(value) -> datetime:
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
    """Validate inputs, parse times, and load queries."""

    ctx.start_time = _parse_time(args.start_time)
    ctx.end_time = _parse_time(args.end_time)

    if ctx.end_time <= ctx.start_time:
        raise ValueError("end_time must be after start_time")

    ctx.prom_namespace = args.prom_namespace or PROM_NAMESPACE
    ctx.prom_url = args.prom_url or PROM_URL
    ctx.prom_service_account = args.prom_service_account or PROM_SERVICE_ACCOUNT

    if args.output_dir is not None:
        ctx.output_dir = Path(args.output_dir)
    else:
        ctx.output_dir = args.artifact_dir / "prometheus_metrics"
    ctx.output_dir.mkdir(parents=True, exist_ok=True)

    queries_path = Path(args.queries_file)
    if not queries_path.exists():
        raise FileNotFoundError(f"Queries file not found: {queries_path}")

    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(queries_path, src_dir / queries_path.name)

    with queries_path.open("r", encoding="utf-8") as f:
        ctx.queries = yaml.safe_load(f)

    if not isinstance(ctx.queries, dict) or not ctx.queries:
        raise ValueError(
            f"Queries file must contain a non-empty name:promql dict, got: {type(ctx.queries)}"
        )

    ctx.start_epoch = f"{ctx.start_time.timestamp():.3f}"
    ctx.end_epoch = f"{ctx.end_time.timestamp():.3f}"
    ctx.pod_name = f"prom-metrics-query-{int(ctx.start_time.timestamp() * 1000)}"

    return (
        f"Loaded {len(ctx.queries)} queries, window: "
        f"{ctx.start_time.isoformat()} -> {ctx.end_time.isoformat()}"
    )


@task
def create_query_pod(args, ctx):
    """Create a temporary curl pod in openshift-monitoring."""

    best_effort_oc("delete", "pod", ctx.pod_name, "-n", ctx.prom_namespace, "--ignore-not-found")

    overrides = json.dumps(
        {
            "spec": {
                "serviceAccountName": ctx.prom_service_account,
                "automountServiceAccountToken": True,
                "terminationGracePeriodSeconds": 1,
            },
        }
    )

    oc(
        "run",
        ctx.pod_name,
        f"--image={CURL_IMAGE}",
        f"--overrides={overrides}",
        "-n",
        ctx.prom_namespace,
        "--restart=Never",
        "--command",
        "--",
        "sleep",
        "600",
    )

    oc(
        "wait",
        "pod",
        ctx.pod_name,
        "-n",
        ctx.prom_namespace,
        "--for=condition=Ready",
        "--timeout=60s",
    )

    return f"Pod {ctx.pod_name} is ready"


@task
def execute_queries(args, ctx):
    """Run each PromQL query via curl inside the pod."""

    succeeded = 0
    failed = 0

    for name, promql in ctx.queries.items():
        logger.info("  querying: %s", name)

        output_path = ctx.output_dir / f"{name}.json"

        quoted_promql = shlex.quote(f"query={promql}")

        curl_cmd = (
            f"curl -sk --max-time 30 --fail-with-body"
            f' -H "Authorization: Bearer $(cat {TOKEN_PATH})"'
            f" {ctx.prom_url}/api/v1/query_range"
            f" --data-urlencode {quoted_promql}"
            f" --data-urlencode start={ctx.start_epoch}"
            f" --data-urlencode end={ctx.end_epoch}"
            f" --data-urlencode step={args.step_seconds}s"
        )

        result = oc_exec(
            "sh",
            "-c",
            curl_cmd,
            namespace=ctx.prom_namespace,
            pod=ctx.pod_name,
            stdout_dest=output_path,
            log_stdout=False,
            check=False,
        )

        if result.success:
            succeeded += 1
        else:
            failed += 1
            logger.warning("  query %s failed: %s", name, result.stderr.strip())

    return f"Executed {succeeded + failed} queries ({succeeded} succeeded, {failed} failed)"


@always
@task
def cleanup_pod(args, ctx):
    """Delete the temporary query pod."""

    pod_name = getattr(ctx, "pod_name", None)
    prom_namespace = getattr(ctx, "prom_namespace", "openshift-monitoring")
    if pod_name:
        best_effort_oc("delete", "pod", pod_name, "-n", prom_namespace, "--ignore-not-found")

    return "Cleaned up query pod"


if __name__ == "__main__":
    run.main()
