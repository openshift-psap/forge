"""Parse and persist native k6 metrics for read and write tests."""

from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path

from projects.mlflow.toolbox.k6 import K6Results

_STATS_HEADER = (
    "Type,Name,Request Count,Requests/s,Average Response Time,"
    "Min Response Time,Max Response Time,Median Response Time,"
    "P90 Response Time,P95 Response Time,P99 Response Time,Failure Count,"
    "HTTP Average Response Time,HTTP P90 Response Time,HTTP P95 Response Time,"
    "HTTP P99 Response Time,HTTP P95 Waiting Time\n"
)
_LATENCY_STATS = ("avg", "min", "max", "med", "p(90)", "p(95)", "p(99)")


def capture_read(
    *,
    results: K6Results,
    output_dir: Path,
    job_name: str,
    operation: str,
) -> None:
    """Write read workload artifacts."""
    summary = _summary_from_log(results.raw_log)
    stats_csv = _read_stats_csv(summary, operation)
    _write_artifacts(summary, stats_csv, results.raw_log, output_dir, job_name)


def capture_write(
    *,
    results: K6Results,
    output_dir: Path,
    job_name: str,
) -> None:
    """Write workload artifacts."""
    summary = _summary_from_log(results.raw_log)
    stats_csv = _write_stats_csv(summary)
    _write_artifacts(summary, stats_csv, results.raw_log, output_dir, job_name)


def get_client_rps(results: K6Results) -> float:
    """Read the aggregate request rate from the k6 summary."""
    metrics = _summary_from_log(results.raw_log).get("metrics", {})
    rate = metrics.get("http_reqs", {}).get("values", {}).get("rate", 0)
    return float(rate or 0)


def _write_artifacts(
    summary: dict,
    stats_csv: str,
    raw_log: str,
    output_dir: Path,
    job_name: str,
) -> None:
    results_dir = output_dir / "artifacts" / "results" / job_name
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "k6.log").write_text(raw_log, encoding="utf-8")
    (results_dir / "k6-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    (results_dir / "stats.csv").write_text(stats_csv, encoding="utf-8")


def _summary_from_log(logs: str) -> dict:
    marker = "===K6_SUMMARY==="
    end_marker = "===K6_SUMMARY_END==="
    if marker not in logs or end_marker not in logs:
        raise RuntimeError("k6 summary marker not found in Job logs")
    raw = logs.split(marker, 1)[1].split(end_marker, 1)[0]
    return json.loads(raw)


def _read_stats_csv(summary: dict, operation: str) -> str:
    metrics = summary.get("metrics", {})
    operation_values = metrics.get("mlflow_read_duration", {}).get("values", {})
    http_values = metrics.get("http_req_duration", {}).get("values", {})
    waiting_values = metrics.get("http_req_waiting", {}).get("values", {})
    requests = metrics.get("mlflow_read_requests", {}).get("values", {}).get("count", 0)
    rate = metrics.get("http_reqs", {}).get("values", {}).get("rate", 0)
    failures = metrics.get("mlflow_read_failures", {}).get("values", {}).get("count", 0)

    return _stats_csv(
        (
            "MLFLOW_READ_HTTP",
            operation,
            requests,
            rate,
            *(operation_values.get(stat) for stat in _LATENCY_STATS),
            failures,
            *(http_values.get(stat) for stat in ("avg", "p(90)", "p(95)", "p(99)")),
            waiting_values.get("p(95)"),
        ),
    )


def _write_stats_csv(summary: dict) -> str:
    """Emit one row per measured MLflow endpoint; setup calls are excluded."""
    metrics = summary.get("metrics", {})

    def row(
        latency_metric: str,
        request_metric: str,
        failure_metric: str,
        endpoint: str,
    ) -> tuple:
        values = metrics.get(latency_metric, {}).get("values", {})
        request_values = metrics.get(request_metric, {}).get("values", {})
        failures = metrics.get(failure_metric, {}).get("values", {}).get("count", 0)
        requests = request_values.get("count", 0)
        latencies = values if requests else {}
        # The built-in HTTP metrics are aggregated across endpoints.
        return (
            "MLFLOW_HTTP",
            endpoint,
            requests,
            request_values.get("rate", 0),
            *(latencies.get(stat) for stat in _LATENCY_STATS),
            failures,
            None,
            None,
            None,
            None,
            None,
        )

    return _stats_csv(
        row(
            "trace_metadata_latency",
            "trace_metadata_requests",
            "trace_metadata_failures",
            "POST /mlflow/api/3.0/mlflow/traces",
        ),
        row(
            "span_latency",
            "span_requests",
            "span_failures",
            "POST /v1/traces",
        ),
    )


def _stats_csv(*rows: tuple) -> str:
    """Write absent values as empty CSV fields."""
    output = StringIO()
    output.write(_STATS_HEADER)
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue()
