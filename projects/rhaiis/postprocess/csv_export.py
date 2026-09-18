"""Generate consolidated_dashboard.csv from PSAP JSON files.

Matches the CSV schema from model-furnace's visualization/parse_furnace_results.py.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

FIELDNAMES = [
    "run",
    "accelerator",
    "model",
    "version",
    "prompt toks",
    "output toks",
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
    "DP",
    "dataset",
    "spec_decoding",
    "prefix_caching",
    "turn",
    "turns",
    "prefix_tokens",
    "prefix_count",
    "request_type",
    "mlflow_run_id",
    "mlflow_experiment_id",
]


def _parse_accelerator_key(acc_key: str) -> tuple[str, str | None]:
    """Return (chip_family, cluster_tag) from an accelerator key.

    Convention: keys follow ``<chip_family>_<cluster_tag>`` (e.g. H200_HERA).
    Keys without an underscore have no cluster tag.
    """
    if not acc_key:
        return "", None
    if "_" in acc_key:
        chip_family, cluster_tag = acc_key.split("_", 1)
        return chip_family, cluster_tag.lower()
    return acc_key, None


def _format_runtime_args(inference_server_args: dict, trtllm_config: dict | None = None) -> str:
    """Format args into semicolon-separated ``key: value`` string matching model-furnace."""
    if not inference_server_args and not trtllm_config:
        return ""
    parts = []
    for key, value in (inference_server_args or {}).items():
        formatted_key = key.replace("_", "-")
        formatted_value = (
            json.dumps(value, separators=(",", ":")) if isinstance(value, dict) else value
        )
        parts.append(f"{formatted_key}: {formatted_value}")
    for key, value in (trtllm_config or {}).items():
        formatted_key = f"trtllm.{key.replace('_', '-')}"
        formatted_value = (
            json.dumps(value, separators=(",", ":")) if isinstance(value, dict) else value
        )
        parts.append(f"{formatted_key}: {formatted_value}")
    return "; ".join(parts)


def find_psap_files(artifact_dir: Path) -> list[Path]:
    """Glob for PSAP_perf*.json files in artifact_dir (recursive)."""
    return sorted(artifact_dir.rglob("PSAP_perf*.json"))


def generate_dashboard_csv(
    psap_json_path: Path, version: str, output_path: Path, *, run_uuid: str = ""
) -> Path:
    """Read a PSAP JSON file and produce a dashboard CSV."""
    with open(psap_json_path, encoding="utf-8") as f:
        payload = json.load(f)

    model = payload.get("model", "")
    acc_key = payload.get("accelerator_type", "")
    acc, cluster_tag = _parse_accelerator_key(acc_key)

    inference_server_args = payload.get("inference_server_args", {})
    tp = (
        inference_server_args.get("tensor-parallel-size")
        or inference_server_args.get("tp-size")
        or inference_server_args.get("tp_size")
        or payload.get("accelerator_count", 1)
    )

    image_tag = payload.get("container_image_tag", "")
    runtime_args = _format_runtime_args(inference_server_args, payload.get("trtllm_config"))

    report = payload.get("report", {})
    guidellm_version = report.get("metadata", {}).get("guidellm_version", "")
    benchmarks = report.get("benchmarks", [])

    data_str = ""
    args = report.get("args", {})
    if isinstance(args, dict):
        data_list = args.get("data", [])
        if data_list:
            data_str = data_list[0] if isinstance(data_list, list) else str(data_list)

    tokens = dict(re.findall(r"(\w+)=([\d.]+)", data_str))
    prompt_toks = int(float(tokens["prompt_tokens"])) if "prompt_tokens" in tokens else ""
    output_toks = int(float(tokens["output_tokens"])) if "output_tokens" in tokens else ""
    turns_count = int(float(tokens["turns"])) if "turns" in tokens else ""
    prefix_tokens_val = int(float(tokens["prefix_tokens"])) if "prefix_tokens" in tokens else ""
    prefix_count_val = int(float(tokens["prefix_count"])) if "prefix_count" in tokens else ""

    start_times = []
    end_times = []
    for bench in benchmarks:
        sched = bench.get("scheduler_metrics", {})
        if "start_time" in sched:
            start_times.append(sched["start_time"])
        if "end_time" in sched:
            end_times.append(sched["end_time"])
    guidellm_start_ms = (
        int(min(start_times) * 1000) if start_times else payload.get("guidellm_start_time_ms")
    )
    guidellm_end_ms = (
        int(max(end_times) * 1000) if end_times else payload.get("guidellm_end_time_ms")
    )

    rows = []
    for bench in benchmarks:
        metrics = bench.get("metrics", {})
        strategy = bench.get("config", {}).get("strategy", {})
        model_name = f"{acc}-{cluster_tag}-{model}-{tp}" if cluster_tag else f"{acc}-{model}-{tp}"

        common_kwargs = dict(
            model_name=model_name,
            model=model,
            accelerator=acc,
            version=version,
            tp=tp,
            prompt_toks=prompt_toks,
            output_toks=output_toks,
            image_tag=image_tag,
            runtime_args=runtime_args,
            guidellm_start_ms=guidellm_start_ms,
            guidellm_end_ms=guidellm_end_ms,
            guidellm_version=guidellm_version,
            run_uuid=run_uuid,
            turns_count=turns_count,
            prefix_tokens_val=prefix_tokens_val,
            prefix_count_val=prefix_count_val,
        )

        row = _extract_row(metrics=metrics, strategy=strategy, **common_kwargs)
        rows.append(row)

        successful_reqs = bench.get("requests", {}).get("successful", [])
        rows.extend(_per_turn_rows(successful_reqs, strategy=strategy, **common_kwargs))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Generated dashboard CSV with %d rows: %s", len(rows), output_path)
    return output_path


def _extract_row(
    *,
    metrics: dict,
    strategy: dict,
    model_name: str,
    model: str,
    accelerator: str,
    version: str,
    tp,
    prompt_toks,
    output_toks,
    image_tag: str,
    runtime_args: str,
    guidellm_start_ms,
    guidellm_end_ms,
    guidellm_version: str,
    run_uuid: str = "",
    turns_count="",
    prefix_tokens_val="",
    prefix_count_val="",
) -> dict:
    def _pct(metric_name: str, pct: str):
        return metrics.get(metric_name, {}).get("successful", {}).get("percentiles", {}).get(pct)

    def _stat(metric_name: str, stat: str):
        return metrics.get(metric_name, {}).get("successful", {}).get(stat)

    def _total_stat(metric_name: str, stat: str):
        return metrics.get(metric_name, {}).get("total", {}).get(stat)

    request_totals = metrics.get("request_totals", {})
    successful = request_totals.get("successful", 0)
    errored = request_totals.get("errored", 0)

    return {
        "run": model_name,
        "accelerator": accelerator,
        "model": model,
        "version": version,
        "prompt toks": prompt_toks,
        "output toks": output_toks,
        "TP": tp,
        "measured concurrency": _stat("request_concurrency", "mean"),
        "intended concurrency": strategy.get("streams"),
        "measured rps": _stat("requests_per_second", "mean"),
        "output_tok/sec": _total_stat("output_tokens_per_second", "mean"),
        "total_tok/sec": _total_stat("tokens_per_second", "mean"),
        "prompt_token_count_mean": _stat("prompt_token_count", "mean"),
        "prompt_token_count_p99": _pct("prompt_token_count", "p99"),
        "output_token_count_mean": _stat("output_token_count", "mean"),
        "output_token_count_p99": _pct("output_token_count", "p99"),
        "ttft_median": _stat("time_to_first_token_ms", "median"),
        "ttft_p95": _pct("time_to_first_token_ms", "p95"),
        "ttft_p1": _pct("time_to_first_token_ms", "p01"),
        "ttft_p999": _pct("time_to_first_token_ms", "p999"),
        "tpot_median": _stat("time_per_output_token_ms", "median"),
        "tpot_p95": _pct("time_per_output_token_ms", "p95"),
        "tpot_p99": _pct("time_per_output_token_ms", "p99"),
        "tpot_p999": _pct("time_per_output_token_ms", "p999"),
        "tpot_p1": _pct("time_per_output_token_ms", "p01"),
        "itl_median": _stat("inter_token_latency_ms", "median"),
        "itl_p95": _pct("inter_token_latency_ms", "p95"),
        "itl_p999": _pct("inter_token_latency_ms", "p999"),
        "itl_p1": _pct("inter_token_latency_ms", "p01"),
        "request_latency_median": _stat("request_latency", "median"),
        "request_latency_min": _stat("request_latency", "min"),
        "request_latency_max": _stat("request_latency", "max"),
        "successful_requests": successful,
        "errored_requests": errored,
        "uuid": run_uuid,
        "ttft_mean": _stat("time_to_first_token_ms", "mean"),
        "ttft_p99": _pct("time_to_first_token_ms", "p99"),
        "itl_mean": _stat("inter_token_latency_ms", "mean"),
        "itl_p99": _pct("inter_token_latency_ms", "p99"),
        "runtime_args": runtime_args,
        "guidellm_start_time_ms": guidellm_start_ms or "",
        "guidellm_end_time_ms": guidellm_end_ms or "",
        "image_tag": image_tag,
        "guidellm_version": guidellm_version,
        "DP": "",
        "dataset": "",
        "spec_decoding": "",
        "prefix_caching": "",
        "turn": "",
        "turns": turns_count,
        "prefix_tokens": prefix_tokens_val,
        "prefix_count": prefix_count_val,
        "request_type": "",
        "mlflow_run_id": "",
        "mlflow_experiment_id": "",
    }


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Linear-interpolation percentile over a pre-sorted list."""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])


def _req_stats(reqs: list[dict], field: str) -> dict:
    """Compute mean/median/min/max/percentiles for a numeric field across requests."""
    vals = sorted(r[field] for r in reqs if r.get(field) is not None)
    if not vals:
        return {}
    n = len(vals)
    return {
        "mean": sum(vals) / n,
        "median": vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2,
        "min": vals[0],
        "max": vals[-1],
        "p01": _percentile(vals, 1),
        "p95": _percentile(vals, 95),
        "p99": _percentile(vals, 99),
        "p999": _percentile(vals, 99.9),
    }


def _per_turn_rows(
    successful_reqs: list[dict],
    *,
    strategy: dict,
    model_name: str,
    model: str,
    accelerator: str,
    version: str,
    tp,
    prompt_toks,
    output_toks,
    image_tag: str,
    runtime_args: str,
    guidellm_start_ms,
    guidellm_end_ms,
    guidellm_version: str,
    run_uuid: str = "",
    turns_count="",
    prefix_tokens_val="",
    prefix_count_val="",
) -> list[dict]:
    """Emit one row per turn from individual request records."""
    if not successful_reqs:
        return []

    by_turn: dict[int, list[dict]] = defaultdict(list)
    for req in successful_reqs:
        turn = req.get("info", {}).get("turn_index", 0)
        by_turn[turn].append(req)

    if len(by_turn) <= 1:
        return []

    rows = []
    for turn_idx in sorted(by_turn):
        reqs = by_turn[turn_idx]

        ttft = _req_stats(reqs, "time_to_first_token_ms")
        itl = _req_stats(reqs, "inter_token_latency_ms")
        tpot = _req_stats(reqs, "time_per_output_token_ms")
        latency = _req_stats(reqs, "request_latency")
        p_toks = _req_stats(reqs, "prompt_tokens")
        o_toks = _req_stats(reqs, "output_tokens")
        otps = _req_stats(reqs, "output_tokens_per_second")
        tps = _req_stats(reqs, "tokens_per_second")

        rows.append(
            {
                "run": model_name,
                "accelerator": accelerator,
                "model": model,
                "version": version,
                "prompt toks": prompt_toks,
                "output toks": output_toks,
                "TP": tp,
                "measured concurrency": "",
                "intended concurrency": strategy.get("streams"),
                "measured rps": "",
                "output_tok/sec": otps.get("mean", ""),
                "total_tok/sec": tps.get("mean", ""),
                "prompt_token_count_mean": p_toks.get("mean", ""),
                "prompt_token_count_p99": p_toks.get("p99", ""),
                "output_token_count_mean": o_toks.get("mean", ""),
                "output_token_count_p99": o_toks.get("p99", ""),
                "ttft_median": ttft.get("median", ""),
                "ttft_p95": ttft.get("p95", ""),
                "ttft_p1": ttft.get("p01", ""),
                "ttft_p999": ttft.get("p999", ""),
                "tpot_median": tpot.get("median", ""),
                "tpot_p95": tpot.get("p95", ""),
                "tpot_p99": tpot.get("p99", ""),
                "tpot_p999": tpot.get("p999", ""),
                "tpot_p1": tpot.get("p01", ""),
                "itl_median": itl.get("median", ""),
                "itl_p95": itl.get("p95", ""),
                "itl_p999": itl.get("p999", ""),
                "itl_p1": itl.get("p01", ""),
                "request_latency_median": latency.get("median", ""),
                "request_latency_min": latency.get("min", ""),
                "request_latency_max": latency.get("max", ""),
                "successful_requests": len(reqs),
                "errored_requests": "",
                "uuid": run_uuid,
                "ttft_mean": ttft.get("mean", ""),
                "ttft_p99": ttft.get("p99", ""),
                "itl_mean": itl.get("mean", ""),
                "itl_p99": itl.get("p99", ""),
                "runtime_args": runtime_args,
                "guidellm_start_time_ms": guidellm_start_ms or "",
                "guidellm_end_time_ms": guidellm_end_ms or "",
                "image_tag": image_tag,
                "guidellm_version": guidellm_version,
                "DP": "",
                "dataset": "",
                "spec_decoding": "",
                "prefix_caching": "",
                "turn": turn_idx,
                "turns": turns_count,
                "prefix_tokens": prefix_tokens_val,
                "prefix_count": prefix_count_val,
                "request_type": "",
                "mlflow_run_id": "",
                "mlflow_experiment_id": "",
            }
        )

    return rows
