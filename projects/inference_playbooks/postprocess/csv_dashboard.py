"""Inference Playbooks CSV dashboard schema and metadata mapper."""

from __future__ import annotations

from typing import Any

PLAYBOOKS_FIELDNAMES = [
    "run",
    "accelerator",
    "model",
    "version",
    "benchmark_tool",
    "workload_key",
    "concurrency",
    "prompt toks",
    "output toks",
    "turn_index",
    "turns",
    "TP",
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
]


def playbooks_metadata_row_mapper(labels: dict[str, Any]) -> dict[str, Any]:
    """Map KPI labels to Playbooks CSV metadata columns."""
    acc = labels.get("accelerator", "").upper()
    model_id = labels.get("hf_model_id", "")
    tp = labels.get("tensor_parallel_size", "1")
    benchmark_tool = labels.get("benchmark_tool", "guidellm")
    workload_key = labels.get("benchmark_key", labels.get("guidellm_loadshape", ""))
    concurrency = labels.get("concurrency", "")

    run_name = f"{acc}-{model_id}-{tp}" if acc else f"{model_id}-{tp}"

    return {
        "run": run_name,
        "accelerator": acc,
        "model": model_id,
        "version": labels.get("version", ""),
        "benchmark_tool": benchmark_tool,
        "workload_key": workload_key,
        "concurrency": concurrency,
        "TP": tp,
        "prompt toks": labels.get("prompt_toks", ""),
        "output toks": labels.get("output_toks", ""),
        "image_tag": labels.get("image_tag", ""),
        "runtime_args": labels.get("runtime_args", ""),
        "uuid": labels.get("run_uuid", ""),
        "guidellm_start_time_ms": labels.get("guidellm_start_time_ms", ""),
        "guidellm_end_time_ms": labels.get("guidellm_end_time_ms", ""),
        "guidellm_version": labels.get("guidellm_version", ""),
        "mlflow_run_id": labels.get("mlflow_run_id", ""),
        "mlflow_experiment_id": labels.get("mlflow_experiment_id", ""),
    }
