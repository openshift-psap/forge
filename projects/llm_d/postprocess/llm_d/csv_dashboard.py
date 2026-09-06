"""LLM_D CSV dashboard export functionality."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi.dataclasses import KpiRecord

DASHBOARD_FIELDNAMES = [
    "run",
    "accelerator",
    "model",
    "version",
    "prompt toks",
    "output toks",
    "TP",
    "DP",
    "EP",
    "replicas",
    "prefill_pod_count",
    "decode_pod_count",
    "router_config",
    "measured concurrency",
    "intended concurrency",
    "measured rps",
    "output_tok/sec",
    "total_tok/sec",
    "prompt_token_count_mean",
    "prompt_token_count_p99",
    "output_token_count_mean",
    "output_token_count_p99",
    "ttft_median",
    "ttft_p95",
    "ttft_p1",
    "ttft_p999",
    "tpot_median",
    "tpot_p95",
    "tpot_p99",
    "tpot_p999",
    "tpot_p1",
    "itl_median",
    "itl_p95",
    "itl_p999",
    "itl_p1",
    "request_latency_median",
    "request_latency_min",
    "request_latency_max",
    "successful_requests",
    "errored_requests",
    "uuid",
    "ttft_mean",
    "ttft_p99",
    "itl_mean",
    "itl_p99",
    "runtime_args",
    "guidellm_start_time_ms",
    "guidellm_end_time_ms",
    "image_tag",
    "guidellm_version",
    "mlflow_run_id",
    "mlflow_experiment_id",
    "notes",
]


def export_kpis_to_csv(
    kpi_records: list[KpiRecord],
    output_path: Path,
    include_header_comments: bool = True,
    *,
    model=None,
) -> str:
    """Export KPI records to CSV format with llm-d dashboard schema.

    This function generates dashboard KPIs independently from the original model data,
    following the e1780be approach while using the new dataclass architecture.
    The main KPI handling and CSV handling are kept separate.

    Args:
        kpi_records: KPI records from compute_kpis() (not used, for interface compatibility)
        output_path: Path where to write the CSV file
        include_header_comments: Whether to include descriptive header comments

    Returns:
        Path to the generated CSV file
    """
    # Get the cached model from the current plugin instance
    # This allows independent dashboard KPI generation for CSV export
    import inspect

    from projects.guidellm.postprocess.guidellm.dashboard import (
        export_dashboard_kpis_to_csv,
        normalize_product_version,
    )

    frame = inspect.currentframe()
    try:
        # Walk up the call stack to find the plugin instance
        plugin_instance = None
        while frame:
            if "self" in frame.f_locals:
                obj = frame.f_locals["self"]
                if hasattr(obj, "_cached_model"):
                    plugin_instance = obj
                    break
            frame = frame.f_back
    finally:
        del frame

    if plugin_instance is None or not hasattr(plugin_instance, "_cached_model"):
        # Fallback: create empty CSV
        import csv

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=DASHBOARD_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
        print("Exported 0 dashboard CSV rows (no cached model available)")
        return str(output_path)

    # Generate dashboard KPIs independently from the cached model
    from projects.guidellm.postprocess.guidellm.dashboard import compute_dashboard_kpis

    model = plugin_instance._cached_model
    dashboard_kpis = compute_dashboard_kpis(model, prefix="llmd")

    def metadata_row(labels: dict[str, Any]) -> dict[str, Any]:
        """Extract metadata for CSV row from dashboard KPI labels."""
        accelerator = labels.get("gpu_type") or labels.get("accelerator", "")
        model_id = labels.get("hf_model_id") or labels.get("model_name", "")
        run_model = model_id.replace("/", "-")
        tp = labels.get("tensor_parallel_size", "")
        replicas = labels.get("replicas", "")
        version = normalize_product_version(
            labels.get("product_version") or labels.get("version", "")
        )
        deployment_profile = labels.get("deployment_profile", "")
        if version and deployment_profile:
            version = f"{version}-{deployment_profile}"

        return {
            "run": "-".join(str(value) for value in (accelerator, run_model, tp) if value),
            "accelerator": accelerator,
            "model": model_id,
            "version": version,
            "prompt toks": labels.get("prompt_toks", ""),
            "output toks": labels.get("output_toks", ""),
            "TP": tp,
            "DP": labels.get("DP") or labels.get("data_parallel_size") or "",
            "EP": labels.get("EP") or labels.get("expert_parallel_size") or "",
            "replicas": replicas,
            "prefill_pod_count": labels.get("prefill_pod_count", ""),
            "decode_pod_count": labels.get("decode_pod_count", ""),
            "router_config": labels.get("router_config", ""),
            "uuid": labels.get("run_uuid", ""),
            "runtime_args": labels.get("runtime_args", ""),
            "guidellm_start_time_ms": labels.get("guidellm_start_time_ms", ""),
            "guidellm_end_time_ms": labels.get("guidellm_end_time_ms", ""),
            "image_tag": labels.get("image_tag", ""),
            "guidellm_version": labels.get("guidellm_version", ""),
            "mlflow_run_id": labels.get("mlflow_run_id", ""),
            "mlflow_experiment_id": labels.get("mlflow_experiment_id", ""),
            "notes": labels.get("notes", ""),
        }

    # Convert KpiRecord dataclasses to dicts for export_dashboard_kpis_to_csv
    dashboard_kpi_dicts = [kpi.to_dict() for kpi in dashboard_kpis]

    return export_dashboard_kpis_to_csv(
        dashboard_kpi_dicts,
        output_path,
        prefix="llmd",
        fieldnames=DASHBOARD_FIELDNAMES,
        metadata_row=metadata_row,
    )
