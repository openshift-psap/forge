"""
Run an aiperf benchmark against a Dynamo (or any OpenAI-compatible) endpoint.

Handles dataset download/caching, aiperf CLI invocation, and artifact collection.
Designed to be used alongside or instead of guidellm for Dynamo inference benchmarks.

Requires `aiperf` to be installed: pip install aiperf
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from projects.core.dsl import entrypoint, execute_tasks, shell, task

logger = logging.getLogger(__name__)

_DATASET_CACHE = Path("/tmp/forge-aiperf-datasets")


@entrypoint
def run(
    *,
    endpoint_url: str,
    model_name: str,
    artifact_dir: Path | None = None,
    # Dataset
    dataset_url: str = "https://raw.githubusercontent.com/kvcache-ai/Mooncake/refs/heads/main/FAST25-release/traces/conversation_trace.jsonl",
    dataset_type: str = "mooncake_trace",
    dataset_cap: int | None = 2000,
    # Endpoint
    endpoint_type: str = "chat",
    endpoint_path: str = "/v1/chat/completions",
    streaming: bool = True,
    # Schedule
    fixed_schedule: bool = True,
    fixed_schedule_auto_offset: bool = True,
    # Limits
    max_seconds: int = 7200,
    synthesis_max_isl: int | None = 131072,
    # aiperf options
    tokenizer: str | None = None,
    export_level: str | None = None,
    export_http_trace: bool = False,
    # Environment
    extra_env: dict[str, str] | None = None,
    namespace: str = "default",
) -> int:
    execute_tasks(locals())
    return 0


@task
def check_aiperf_installed(args, ctx):
    """Verify aiperf CLI is available."""
    result = subprocess.run(["which", "aiperf"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "aiperf not found. Install with: pip install aiperf"
        )
    version = subprocess.run(
        ["aiperf", "--version"], capture_output=True, text=True
    )
    ctx.aiperf_version = (version.stdout or "").strip()
    logger.info("aiperf found: %s", ctx.aiperf_version or "version unknown")


@task
def download_dataset(args, ctx):
    """Download and optionally cap the benchmark dataset."""
    _DATASET_CACHE.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(args.dataset_url)
    filename = Path(parsed.path).name or "dataset.jsonl"
    dataset_path = _DATASET_CACHE / filename

    if dataset_path.exists():
        logger.info("Using cached dataset: %s", dataset_path)
    else:
        logger.info("Downloading dataset from %s", args.dataset_url)
        import requests
        with requests.get(args.dataset_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            temp = dataset_path.with_suffix(".download")
            with temp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            temp.replace(dataset_path)
        logger.info("Downloaded dataset to %s", dataset_path)

    if args.dataset_cap:
        capped = dataset_path.with_name(f"{dataset_path.stem}-cap{args.dataset_cap}{dataset_path.suffix}")
        if capped.exists():
            logger.info("Using cached capped dataset: %s (%d entries)", capped.name, args.dataset_cap)
        else:
            logger.info("Capping dataset to %d entries", args.dataset_cap)
            written = 0
            with dataset_path.open("r") as src, capped.open("w") as dst:
                for line in src:
                    if not line.strip():
                        continue
                    dst.write(line)
                    written += 1
                    if written >= args.dataset_cap:
                        break
            logger.info("Wrote capped dataset: %d entries", written)
        ctx.dataset_path = capped
    else:
        ctx.dataset_path = dataset_path


@task
def run_aiperf(args, ctx):
    """Execute the aiperf profile command."""
    output_dir = args.artifact_dir / "aiperf" if args.artifact_dir else Path("/tmp/forge-aiperf-output")
    output_dir.mkdir(parents=True, exist_ok=True)
    ctx.output_dir = output_dir

    tokenizer = args.tokenizer or args.model_name

    command = [
        "aiperf", "profile",
        "--model", args.model_name,
        "--url", args.endpoint_url,
        "--endpoint-type", args.endpoint_type,
        "--endpoint", args.endpoint_path,
        "--input-file", str(ctx.dataset_path),
        "--custom-dataset-type", args.dataset_type,
        "--tokenizer", tokenizer,
        "--artifact-dir", str(output_dir),
        "--ui", "none",
    ]

    if args.streaming:
        command.append("--streaming")
    if args.fixed_schedule:
        command.append("--fixed-schedule")
    if args.fixed_schedule_auto_offset:
        command.append("--fixed-schedule-auto-offset")
    if args.synthesis_max_isl is not None:
        command.extend(["--synthesis-max-isl", str(args.synthesis_max_isl)])
    if args.export_level:
        command.extend(["--export-level", args.export_level])
    if args.export_http_trace:
        command.append("--export-http-trace")

    env = dict(os.environ)
    # Isolate aiperf runtime dirs
    runtime_root = Path("/tmp/forge-aiperf-runtime")
    for d in ["home", "huggingface", "xdg-cache"]:
        (runtime_root / d).mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(runtime_root / "home"),
        "HF_HOME": str(runtime_root / "huggingface"),
        "XDG_CACHE_HOME": str(runtime_root / "xdg-cache"),
        "AIPERF_HTTP_SSL_VERIFY": "false",
    })
    if args.extra_env:
        env.update(args.extra_env)

    logger.info("Running aiperf: %s", " ".join(command))
    logger.info("Output dir: %s", output_dir)

    result = subprocess.run(command, env=env, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"aiperf exited with status {result.returncode}")

    logger.info("aiperf completed successfully")


@task
def parse_results(args, ctx):
    """Parse aiperf results and log summary."""
    summary_path = ctx.output_dir / "profile_export_aiperf.json"
    if not summary_path.exists():
        logger.warning("aiperf did not produce profile_export_aiperf.json")
        return

    summary = json.loads(summary_path.read_text())
    ctx.summary = summary

    metrics = {
        "request_throughput": _metric(summary, "request_throughput", "avg"),
        "output_token_throughput": _metric(summary, "output_token_throughput", "avg"),
        "ttft_avg_ms": _metric(summary, "time_to_first_token", "avg"),
        "ttft_p95_ms": _metric(summary, "time_to_first_token", "p95"),
        "itl_avg_ms": _metric(summary, "inter_token_latency", "avg"),
        "itl_p95_ms": _metric(summary, "inter_token_latency", "p95"),
        "request_latency_avg_ms": _metric(summary, "request_latency", "avg"),
        "request_count": summary.get("request_count"),
        "error_count": summary.get("error_request_count"),
        "total_output_tokens": summary.get("total_output_tokens"),
    }

    logger.info("=== aiperf Results ===")
    for key, value in metrics.items():
        if value is not None:
            logger.info("  %s: %s", key, f"{value:.2f}" if isinstance(value, float) else value)

    # Write summary to artifact dir
    if args.artifact_dir:
        summary_out = args.artifact_dir / "artifacts" / "aiperf_summary.json"
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.write_text(json.dumps(metrics, indent=2))
        logger.info("Summary saved to %s", summary_out)


def _metric(summary: dict, key: str, field: str = "avg") -> float | None:
    value = summary.get(key)
    if isinstance(value, dict):
        m = value.get(field)
        if m is None:
            return None
        try:
            return float(m)
        except (TypeError, ValueError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
