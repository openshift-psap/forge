"""AI Perf profile and execution adapter for llm-d."""

from __future__ import annotations

import copy
import logging
from typing import Any

from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf_command
from projects.core.dsl.utils import slugify_identifier
from projects.core.library import env
from projects.llm_d.orchestration import runtime_config
from projects.llm_d.orchestration.loadgenerator.base import BenchmarkContext, LlmDLoadGenerator

logger = logging.getLogger(__name__)


class AIPerfGenerator(LlmDLoadGenerator):
    tool = "aiperf"

    def resolve_config(self, profile: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        """Apply AI Perf defaults without inheriting GuideLLM settings."""
        return runtime_config.deep_merge(defaults.get("aiperf", {}), copy.deepcopy(profile))

    def expand(self, benchmark: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Expand a concurrency list into sequential single-concurrency runs.

        AI Perf's AgentX MVP scenario requires a single concurrency value.
        Cache isolation across runs is handled by AI Perf's built-in
        ``--cache-bust first_turn_prefix`` mechanism.
        """
        concurrency = benchmark.get("concurrency")
        if not isinstance(concurrency, list):
            return [("default", benchmark)]

        runs = []
        for c in concurrency:
            run_cfg = copy.deepcopy(benchmark)
            run_cfg["concurrency"] = c
            run_cfg["job_name"] = f"{benchmark['job_name']}-c{c}"
            runs.append((f"concurrency-{c}", run_cfg))
        logger.info("Expanded concurrency sweep into %d runs: %s", len(runs), concurrency)
        return runs

    def run(self, context: BenchmarkContext) -> None:
        """Run AI Perf through its standalone toolbox command."""
        benchmark = context.benchmark
        artifact_name = f"benchmark_{slugify_identifier(context.benchmark_key, max_length=48)}"
        with env.NextArtifactDir(artifact_name):
            aiperf_command.run(
                endpoint_url=context.endpoint_url,
                model_name=runtime_config.get_served_model_name(),
                namespace=context.namespace,
                name=benchmark["job_name"],
                image=benchmark["image"],
                timeout=benchmark["timeout_seconds"],
                pvc_size=benchmark["pvc_size"],
                pvc_storage_class=benchmark.get("pvc_storage_class"),
                tokenizer=benchmark.get("tokenizer") or context.model_name,
                endpoint_type=benchmark.get("endpoint_type", "chat"),
                endpoint_path=benchmark.get("endpoint_path", "/v1/chat/completions"),
                streaming=benchmark.get("streaming", True),
                concurrency=benchmark.get("concurrency"),
                request_rate=benchmark.get("request_rate"),
                request_count=benchmark.get("request_count"),
                input_tokens=benchmark.get("input_tokens"),
                output_tokens=benchmark.get("output_tokens"),
                dataset_url=benchmark.get("dataset_url"),
                dataset_sha256=benchmark.get("dataset_sha256"),
                dataset_type=benchmark.get("dataset_type"),
                fixed_schedule=benchmark.get("fixed_schedule", False),
                fixed_schedule_auto_offset=benchmark.get("fixed_schedule_auto_offset", False),
                image_pull_secret=benchmark.get("image_pull_secret"),
                ssl_verify=benchmark.get("ssl_verify", True),
                aiperf_args=benchmark.get("aiperf_args"),
            )
