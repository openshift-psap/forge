"""Shared GuideLLM helpers for dashboard-compatible CSV exports."""

from __future__ import annotations

import csv
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi import (
    KpiCatalogEntry,
    KpiRecord,
)
from projects.caliper.engine.model import (
    ParseResult,
    TestBaseNode,
    UnifiedResultRecord,
    UnifiedRunModel,
)

logger = logging.getLogger(__name__)


# suffix, curve key, dashboard column, unit, higher-is-better
DASHBOARD_METRICS: tuple[tuple[str, str, str, str, bool | None], ...] = (
    ("output_tok_per_sec", "output_tok_per_sec", "output_tok/sec", "tokens/s", True),
    ("total_tok_per_sec", "total_tok_per_sec", "total_tok/sec", "tokens/s", True),
    ("measured_concurrency", "request_concurrency", "measured concurrency", "count", None),
    ("measured_rps", "measured_rps", "measured rps", "req/s", True),
    ("intended_concurrency", "intended_concurrency", "intended concurrency", "count", None),
    ("completed_requests", "successful_requests", "successful_requests", "count", True),
    ("failed_requests", "errored_requests", "errored_requests", "count", False),
    ("ttft_median", "ttft_median", "ttft_median", "s", False),
    ("ttft_p95", "ttft_p95", "ttft_p95", "s", False),
    ("ttft_p99", "ttft_p99", "ttft_p99", "s", False),
    ("ttft_p1", "ttft_p1", "ttft_p1", "s", False),
    ("ttft_p999", "ttft_p999", "ttft_p999", "s", False),
    ("ttft_mean", "ttft_mean", "ttft_mean", "s", False),
    ("tpot_median", "tpot_median", "tpot_median", "s", False),
    ("tpot_p95", "tpot_p95", "tpot_p95", "s", False),
    ("tpot_p99", "tpot_p99", "tpot_p99", "s", False),
    ("tpot_p1", "tpot_p1", "tpot_p1", "s", False),
    ("tpot_p999", "tpot_p999", "tpot_p999", "s", False),
    ("itl_median", "itl_median", "itl_median", "s", False),
    ("itl_p95", "itl_p95", "itl_p95", "s", False),
    ("itl_p99", "itl_p99", "itl_p99", "s", False),
    ("itl_p1", "itl_p1", "itl_p1", "s", False),
    ("itl_p999", "itl_p999", "itl_p999", "s", False),
    ("itl_mean", "itl_mean", "itl_mean", "s", False),
    ("request_latency_median", "request_latency_median", "request_latency_median", "s", False),
    ("request_latency_min", "request_latency_min", "request_latency_min", "s", False),
    ("request_latency_max", "request_latency_max", "request_latency_max", "s", False),
    (
        "prompt_token_count_mean",
        "prompt_token_count_mean",
        "prompt_token_count_mean",
        "tokens",
        None,
    ),
    ("prompt_token_count_p99", "prompt_token_count_p99", "prompt_token_count_p99", "tokens", None),
    (
        "output_token_count_mean",
        "output_token_count_mean",
        "output_token_count_mean",
        "tokens",
        None,
    ),
    ("output_token_count_p99", "output_token_count_p99", "output_token_count_p99", "tokens", None),
)

SECONDS_TO_MS_COLUMNS = frozenset(
    column
    for _, _, column, unit, _ in DASHBOARD_METRICS
    if unit == "s" and not column.startswith("request_latency_")
)

# Metadata that can be emitted as dashboard labels. Runtime/test labels are
# merged last by ``dashboard_metadata_labels`` and therefore take precedence
# over values recovered from artifacts.
DASHBOARD_METADATA_LABEL_KEYS = frozenset(
    {
        "product_version",
        "deployment_profile",
        "model_name",
        "hf_model_id",
        "cluster",
        "benchmark_key",
        "replicas",
        "tensor_parallel_size",
        "runtime_args",
        "image_tag",
        "router_config",
        "gpu_type",
        "mlflow_run_id",
        "mlflow_experiment_id",
    }
)


def canonical_json(value: Any) -> str:
    """Serialize structured metadata deterministically for labels and CSVs."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def normalize_product_version(value: Any) -> str:
    """Normalize RHOAI/KServe versions to the dashboard naming convention."""
    text = str(value or "")
    match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+-ea\.(\d+)", text, re.IGNORECASE)
    if match:
        major, minor, early_access = match.groups()
        return f"RHOAI-{major}.{minor}-EA{early_access}"
    return text


def deployment_metadata_from_profile(
    profile: dict[str, Any], *, profile_name: str | None = None
) -> dict[str, Any]:
    """Extract shared deployment metadata from a resolved deployment profile."""
    metadata: dict[str, Any] = {}
    if profile_name:
        metadata["deployment_profile"] = profile_name
    if "scheduler" in profile:
        metadata["router_config"] = canonical_json(profile["scheduler"])
    elif profile.get("scheduler_manifest"):
        metadata["router_config"] = canonical_json(
            {"scheduler_manifest": profile["scheduler_manifest"]}
        )
    return metadata


def dashboard_metadata_labels(record_metrics: dict[str, Any]) -> dict[str, str]:
    """Build metadata labels with explicit runtime KPI labels taking precedence."""
    labels = {
        key: str(value)
        for key, value in record_metrics.items()
        if key in DASHBOARD_METADATA_LABEL_KEYS and value is not None
    }
    kpi_labels = record_metrics.get("kpi_labels", {})
    if isinstance(kpi_labels, dict):
        labels.update({key: str(value) for key, value in kpi_labels.items()})
    return labels


def validate_dashboard_fieldnames(fieldnames: list[str] | tuple[str, ...]) -> None:
    """Reject CSV schemas that would silently drop a dashboard metric."""
    missing = sorted({column for *_, column, _, _ in DASHBOARD_METRICS} - set(fieldnames))
    if missing:
        raise ValueError(f"Dashboard CSV fieldnames omit dashboard metrics: {', '.join(missing)}")


def _successful_stat(metrics: dict[str, Any], name: str, key: str) -> Any:
    return metrics.get(name, {}).get("successful", {}).get(key)


def _successful_percentile(metrics: dict[str, Any], name: str, key: str) -> Any:
    return metrics.get(name, {}).get("successful", {}).get("percentiles", {}).get(key)


def _milliseconds_to_seconds(value: Any) -> Any:
    return value / 1000.0 if value is not None else None


def enrich_guidellm_parse_result(
    base_result: ParseResult, nodes: list[TestBaseNode]
) -> ParseResult:
    """Preserve dashboard metrics from raw GuideLLM files on parsed records."""
    nodes_by_path = {str(node.test_path): node for node in nodes}
    records: list[UnifiedResultRecord] = []
    for record in base_result.records:
        node = nodes_by_path.get(record.test_base_path)
        if node is None or record.metrics.get("no_benchmarks_found"):
            records.append(record)
            continue
        extra, curves = _extract_dashboard_metrics(node)
        metrics = {**record.metrics, **extra}
        metrics["performance_curves"] = {
            **metrics.get("performance_curves", {}),
            **curves,
        }
        records.append(
            UnifiedResultRecord(
                test_base_path=record.test_base_path,
                distinguishing_labels=record.distinguishing_labels,
                metrics=metrics,
                run_identity=record.run_identity,
                parse_notes=record.parse_notes,
            )
        )
    return ParseResult(records=records, warnings=base_result.warnings)


def _extract_dashboard_metrics(node: TestBaseNode) -> tuple[dict[str, Any], dict[str, list]]:
    files = sorted(
        path
        for path in node.artifact_paths
        if path.name == "benchmarks.json"
        or (path.name.startswith("benchmarks-rate-") and path.suffix == ".json")
    )
    benchmarks: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    args: dict[str, Any] = {}
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        benchmarks.extend(payload.get("benchmarks", []))
        metadata = metadata or payload.get("metadata", {})
        args = args or payload.get("args", {})
    if not benchmarks:
        return {}, {}

    benchmarks.sort(
        key=lambda benchmark: float(
            benchmark.get("metrics", {})
            .get("requests_per_second", {})
            .get("successful", {})
            .get("mean", 0)
            or 0
        )
    )
    data_values = args.get("data", []) if isinstance(args, dict) else []
    if not data_values:
        fallback_data = (
            benchmarks[0]
            .get("benchmarker", {})
            .get("requests", {})
            .get("attributes", {})
            .get("data")
        )
        data_values = [fallback_data] if fallback_data else []
    data_value = data_values[0] if isinstance(data_values, list) and data_values else data_values
    tokens: dict[str, Any] = {}
    if isinstance(data_value, dict):
        tokens = data_value
    elif data_value:
        data_text = str(data_value)
        try:
            parsed_data = json.loads(data_text)
            if isinstance(parsed_data, dict):
                tokens = parsed_data
        except json.JSONDecodeError:
            tokens = dict(re.findall(r"(\w+)=([\d.]+)", data_text))
    starts = [
        b.get("scheduler_metrics", {}).get("start_time", b.get("start_time")) for b in benchmarks
    ]
    ends = [b.get("scheduler_metrics", {}).get("end_time", b.get("end_time")) for b in benchmarks]
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    extra = {
        "guidellm_version": metadata.get("guidellm_version", ""),
        "prompt_toks": int(float(tokens["prompt_tokens"])) if "prompt_tokens" in tokens else "",
        "output_toks": int(float(tokens["output_tokens"])) if "output_tokens" in tokens else "",
        "turns": int(float(tokens["turns"])) if "turns" in tokens else "",
        "prefix_tokens": int(float(tokens["prefix_tokens"])) if "prefix_tokens" in tokens else "",
        "prefix_count": int(float(tokens["prefix_count"])) if "prefix_count" in tokens else "",
        "request_type": args.get("request_type", "") if isinstance(args, dict) else "",
        "guidellm_start_time_ms": int(min(starts) * 1000) if starts else "",
        "guidellm_end_time_ms": int(max(ends) * 1000) if ends else "",
    }
    curves = {curve_key: [] for _, curve_key, _, _, _ in DASHBOARD_METRICS}
    run_uuids: list[str] = []
    for benchmark in benchmarks:
        metrics = benchmark.get("metrics", {})
        strategy = benchmark.get("config", {}).get("strategy", {}) or benchmark.get(
            "scheduler", {}
        ).get("strategy", {})

        totals = (
            benchmark.get("scheduler_metrics", {}).get("requests_made", {})
            or benchmark.get("request_totals", {})
            or benchmark.get("run_stats", {}).get("requests_made", {})
            or metrics.get("request_totals", {})
        )
        run_uuids.append(
            str(benchmark.get("config", {}).get("run_id") or benchmark.get("run_id") or "")
        )
        values = {
            "output_tok_per_sec": metrics.get("output_tokens_per_second", {})
            .get("total", {})
            .get("mean"),
            "total_tok_per_sec": metrics.get("tokens_per_second", {}).get("total", {}).get("mean"),
            "request_concurrency": _successful_stat(metrics, "request_concurrency", "mean"),
            "measured_rps": _successful_stat(metrics, "requests_per_second", "mean"),
            "intended_concurrency": strategy.get("streams", strategy.get("max_concurrency")),
            "successful_requests": totals.get("successful", 0),
            "errored_requests": totals.get("errored", 0),
            "ttft_median": _milliseconds_to_seconds(
                _successful_stat(metrics, "time_to_first_token_ms", "median")
            ),
            "ttft_p95": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_to_first_token_ms", "p95")
            ),
            "ttft_p99": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_to_first_token_ms", "p99")
            ),
            "ttft_p1": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_to_first_token_ms", "p01")
            ),
            "ttft_p999": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_to_first_token_ms", "p999")
            ),
            "ttft_mean": _milliseconds_to_seconds(
                _successful_stat(metrics, "time_to_first_token_ms", "mean")
            ),
            "tpot_median": _milliseconds_to_seconds(
                _successful_stat(metrics, "time_per_output_token_ms", "median")
            ),
            "tpot_p95": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_per_output_token_ms", "p95")
            ),
            "tpot_p99": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_per_output_token_ms", "p99")
            ),
            "tpot_p1": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_per_output_token_ms", "p01")
            ),
            "tpot_p999": _milliseconds_to_seconds(
                _successful_percentile(metrics, "time_per_output_token_ms", "p999")
            ),
            "itl_median": _milliseconds_to_seconds(
                _successful_stat(metrics, "inter_token_latency_ms", "median")
            ),
            "itl_p95": _milliseconds_to_seconds(
                _successful_percentile(metrics, "inter_token_latency_ms", "p95")
            ),
            "itl_p99": _milliseconds_to_seconds(
                _successful_percentile(metrics, "inter_token_latency_ms", "p99")
            ),
            "itl_p1": _milliseconds_to_seconds(
                _successful_percentile(metrics, "inter_token_latency_ms", "p01")
            ),
            "itl_p999": _milliseconds_to_seconds(
                _successful_percentile(metrics, "inter_token_latency_ms", "p999")
            ),
            "itl_mean": _milliseconds_to_seconds(
                _successful_stat(metrics, "inter_token_latency_ms", "mean")
            ),
            "request_latency_median": _successful_stat(metrics, "request_latency", "median"),
            "request_latency_min": _successful_stat(metrics, "request_latency", "min"),
            "request_latency_max": _successful_stat(metrics, "request_latency", "max"),
            "prompt_token_count_mean": _successful_stat(metrics, "prompt_token_count", "mean"),
            "prompt_token_count_p99": _successful_percentile(metrics, "prompt_token_count", "p99"),
            "output_token_count_mean": _successful_stat(metrics, "output_token_count", "mean"),
            "output_token_count_p99": _successful_percentile(metrics, "output_token_count", "p99"),
        }
        for key in curves:
            curves[key].append(values.get(key))
    extra["run_uuids"] = run_uuids
    return extra, curves


def compute_dashboard_kpis(model: UnifiedRunModel, *, prefix: str) -> list[KpiRecord]:
    """Generate curve KPIs for dashboard metrics."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    output: list[KpiRecord] = []

    for record in model.unified_result_records:
        curves = record.metrics.get("performance_curves", {})
        rates = record.metrics.get("request_rate", [])
        if not record.run_identity.get("guidellm") or not rates:
            continue

        # Base labels for this run including operational data
        labels = {**record.distinguishing_labels}
        metadata_labels = {
            "guidellm_version": str(record.metrics.get("guidellm_version", "")),
            "prompt_toks": str(record.metrics.get("prompt_toks", "")),
            "output_toks": str(record.metrics.get("output_toks", "")),
            "turns": str(record.metrics.get("turns", "")),
            "prefix_tokens": str(record.metrics.get("prefix_tokens", "")),
            "prefix_count": str(record.metrics.get("prefix_count", "")),
            "request_type": str(record.metrics.get("request_type", "")),
            # Operational properties now in labels to avoid hierarchical metadata overwrite
            "guidellm_start_time_ms": str(record.metrics.get("guidellm_start_time_ms", "")),
            "guidellm_end_time_ms": str(record.metrics.get("guidellm_end_time_ms", "")),
            "runtime_args": str(
                record.distinguishing_labels.get("runtime_args", "")
            ),  # From test labels
            "mlflow_run_id": str(record.metrics.get("mlflow_run_id", "")),
            "mlflow_experiment_id": str(record.metrics.get("mlflow_experiment_id", "")),
        }
        metadata_labels.update(dashboard_metadata_labels(record.metrics))

        # Simple metadata for hierarchical compatibility
        metadata_dict = {
            "run_path": record.test_base_path,
        }

        # Create curve KPI for each metric
        for suffix, curve_key, _, unit, higher_is_better in DASHBOARD_METRICS:
            curve_values = curves.get(curve_key, [])
            if not curve_values or all(v is None for v in curve_values):
                continue

            # Create [intended_concurrency, metric_value] pairs using intended concurrency from strategy
            curve_data = []
            intended_concurrency_values = curves.get("intended_concurrency", [])

            # Track data quality for warnings
            total_values = len([v for v in curve_values if v is not None])
            skipped_count = 0

            for i, value in enumerate(curve_values):
                if value is None:
                    continue

                if (
                    i < len(intended_concurrency_values)
                    and intended_concurrency_values[i] is not None
                ):
                    x_value = float(intended_concurrency_values[i])
                    curve_data.append([x_value, float(value)])
                else:
                    skipped_count += 1

            # Log warnings for data quality issues
            if not intended_concurrency_values:
                logger.warning(
                    f"Curve KPI '{suffix}' for {record.test_base_path}: no intended_concurrency values found, skipping all {total_values} data points"
                )
            elif skipped_count > 0:
                logger.warning(
                    f"Curve KPI '{suffix}' for {record.test_base_path}: skipped {skipped_count}/{total_values} data points due to missing intended_concurrency values"
                )

            if not curve_data:
                continue

            kpi_labels = dict(labels)
            kpi_labels.update(metadata_labels)

            # Create curve KPI record
            kpi_record = KpiRecord(
                schema_version="1",
                kpi_id=f"{prefix}_{suffix}",
                values=curve_data,  # [[x, y], [x, y], ...] format
                x_unit="rps",
                y_unit=unit,
                x_help="Request rate (concurrency)",
                y_help=f"Dashboard metric: {suffix}",
                run_id=record.test_base_path,
                timestamp=timestamp,
                labels=kpi_labels,
                metadata=metadata_dict,
                is_curve=True,
                higher_is_better=higher_is_better if higher_is_better is not None else True,
            )
            output.append(kpi_record)

    return output


def dashboard_kpi_catalog(*, prefix: str) -> list[KpiCatalogEntry]:
    """Return catalog entries for the shared dashboard KPI set using dataclasses."""
    catalog_entries = []
    for suffix, _, _, unit, higher_is_better in DASHBOARD_METRICS:
        catalog_entry = KpiCatalogEntry(
            kpi_id=f"{prefix}_{suffix}",
            name=f"{prefix}_{suffix}",
            y_unit=unit,  # For curve KPIs, use y_unit
            x_unit="rps",  # Concurrency/request rate
            higher_is_better=higher_is_better if higher_is_better is not None else True,
            is_curve=True,  # Now curve KPIs
            help=f"Dashboard curve metric: {suffix}",
            x_help="Request rate (concurrency)",
            y_help=f"Performance metric: {suffix}",
        )
        catalog_entries.append(catalog_entry)
    return catalog_entries


def export_dashboard_kpis_to_csv(
    kpi_records: list[KpiRecord],
    output_path: Path,
    *,
    prefix: str,
    fieldnames: list[str],
    metadata_row: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    """Extract dashboard metrics from curve KPIs and export to CSV."""
    validate_dashboard_fieldnames(fieldnames)

    # Group KPIs by run_id to process runs together
    runs_by_id: dict[str, list[dict[str, Any]]] = {}

    for kpi in kpi_records:
        # Handle both KpiRecord objects and dictionaries
        if hasattr(kpi, "to_dict"):
            kpi_dict = kpi.to_dict()
        elif isinstance(kpi, dict):
            kpi_dict = kpi
        else:
            # Skip invalid entries (might be tuple or other unexpected types)
            continue

        run_id = kpi_dict.get("run_id", "")
        runs_by_id.setdefault(run_id, []).append(kpi_dict)

    rows: list[dict[str, Any]] = []

    for _run_id, run_kpis in runs_by_id.items():
        # Find curve KPIs for this run
        curve_kpis = {
            kpi["kpi_id"]: kpi
            for kpi in run_kpis
            if kpi.get("is_curve", False) and kpi.get("values")
        }

        if not curve_kpis:
            continue  # Skip runs without curve data

        # Get representative labels from any KPI in this run
        representative_kpi = run_kpis[0]
        labels = representative_kpi.get("labels", {})
        metadata = representative_kpi.get("metadata", {})

        # Merge metadata into labels for backward compatibility with metadata_row functions
        combined_labels = {**labels, **metadata}

        # Map KPI IDs to dashboard columns
        kpi_to_column = {
            f"{prefix}_{suffix}": column for suffix, _, column, _, _ in DASHBOARD_METRICS
        }

        # Extract rate points from primary curve KPI to determine concurrency levels
        primary_kpi = next(iter(curve_kpis.values()))
        values = primary_kpi.get("values", [])  # [[x, y], [x, y], ...]

        # Create one row per concurrency level
        for concurrency, _ in values:
            row: dict[str, Any] = dict.fromkeys(fieldnames, "")
            row.update(metadata_row(combined_labels))

            # Set intended concurrency from the x-value (concurrency level)
            if "intended concurrency" in fieldnames:
                row["intended concurrency"] = int(concurrency)

            # Fill in data from each curve KPI
            for kpi_id, kpi in curve_kpis.items():
                column = kpi_to_column.get(kpi_id)
                if column and column in fieldnames:
                    # Find the value for this concurrency level
                    for conc, value in kpi["values"]:
                        if conc == concurrency:
                            row[column] = float(value)
                            break

            # Convert seconds to milliseconds for timing metrics
            for column in SECONDS_TO_MS_COLUMNS:
                if column in row and row[column] not in ("", None):
                    try:
                        row[column] = float(row[column]) * 1000
                    except (ValueError, TypeError):
                        pass

            rows.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Exported %d dashboard CSV rows to %s", len(rows), output_path)
    return str(output_path)
