"""AI Perf profile and execution adapter for llm-d."""

from __future__ import annotations

import copy
from typing import Any

from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf_command
from projects.core.dsl.utils import slugify_identifier
from projects.core.library import env
from projects.llm_d.orchestration import runtime_config
from projects.llm_d.orchestration.loadgenerator.base import BenchmarkContext, LlmDLoadGenerator


class AIPerfGenerator(LlmDLoadGenerator):
    tool = "aiperf"

    def resolve_config(self, profile: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        """Apply AI Perf defaults without inheriting GuideLLM settings."""
        return runtime_config.deep_merge(defaults.get("aiperf", {}), copy.deepcopy(profile))

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
            )
