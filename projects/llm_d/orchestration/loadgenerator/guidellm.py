"""GuideLLM profile and execution adapter for llm-d orchestration."""

from __future__ import annotations

import copy
from typing import Any

from projects.core.dsl.utils import slugify_identifier
from projects.core.library import env
from projects.guidellm.library import benchconf as benchconf_lib
from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark import main as benchmark_command
from projects.llm_d.orchestration import runtime_config

from .base import BenchmarkContext, LlmDLoadGenerator


class GuideLLMGenerator(LlmDLoadGenerator):
    tool = "guidellm"

    def resolve_config(self, profile: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        """Inherit only the GuideLLM defaults and arguments."""
        benchmark = copy.deepcopy(profile)
        for key in ("job_name", "image", "pvc_size", "pvc_storage_class", "timeout_seconds"):
            if key in defaults and key not in benchmark:
                benchmark[key] = copy.deepcopy(defaults[key])

        if "vllm_args" not in benchmark:
            default_benchmark = defaults.get("benchmarks", {}).get("default", {})
            if "vllm_args" in default_benchmark:
                benchmark["vllm_args"] = copy.deepcopy(default_benchmark["vllm_args"])

        workload_args = defaults.get("args", {})
        if workload_args:
            benchmark["args"] = runtime_config.deep_merge(workload_args, benchmark.get("args", {}))
        return benchmark

    def run(self, context: BenchmarkContext) -> None:
        """Build GuideLLM arguments and run its standalone toolbox command."""
        benchmark = context.benchmark
        config_path = None
        benchconf_ref = benchmark.get("benchconf")
        if benchconf_ref and benchconf_lib._is_enabled():
            benchconf_lib.maybe_install_custom_version()
            config_path = benchconf_lib.resolve_config_path(benchconf_ref)
            benchconf_lib.save_version()

        guidellm_args = build_guidellm_args(benchmark)
        if not any(arg.startswith(("--tokenizer=", "--processor=")) for arg in guidellm_args):
            guidellm_args.append(f"--tokenizer=kind=huggingface_auto,model={context.model_name}")

        artifact_name = f"benchmark_{slugify_identifier(context.benchmark_key, max_length=48)}"
        with env.NextArtifactDir(artifact_name):
            benchmark_command.run(
                endpoint_url=context.endpoint_url,
                name=benchmark.get("job_name"),
                namespace=context.namespace,
                image=benchmark.get("image"),
                timeout=benchmark.get("timeout_seconds"),
                pvc_size=benchmark.get("pvc_size"),
                pvc_storage_class=benchmark.get("pvc_storage_class"),
                guidellm_args=guidellm_args,
                config_path=config_path,
                fs_group=(context.workload or {}).get("fs_group"),
                use_pvc=benchmark.get("use_pvc"),
            )
