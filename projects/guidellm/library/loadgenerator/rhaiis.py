"""GuideLLM execution, warmup, and profiler steps for RHAIIS."""

from __future__ import annotations

import logging

from projects.core.library import config
from projects.guidellm.library.runner import (
    DEFAULT_IMAGE,
    GuideLLMJob,
    build_rate_benchmark_args,
    job_name,
)
from projects.rhaiis.orchestration import runtime_config
from projects.rhaiis.orchestration.loadgenerator.base import BenchmarkContext, RhaiisLoadGenerator

logger = logging.getLogger(__name__)


def _profiler_label(workload: dict) -> str:
    data = workload.get("data", "")
    params = dict(item.split("=", 1) for item in data.split(",") if "=" in item)
    return f"isl{params.get('prompt_tokens', '0')}_osl{params.get('output_tokens', '0')}"


class GuideLLMGenerator(RhaiisLoadGenerator):
    tool = "guidellm"

    def _run_job(
        self,
        context: BenchmarkContext,
        *,
        prefix: str,
        rates: list[int],
        max_seconds: int,
        rampup: int | None = None,
    ) -> None:
        args = build_rate_benchmark_args(
            benchmark_cfg=context.benchmark_cfg,
            model_id=context.model_cfg["hf_model_id"],
            data=context.workload["data"],
            rates=rates,
            max_seconds=max_seconds,
            rampup=rampup,
        )
        GuideLLMJob(
            endpoint_url=f"{context.endpoint_url}/v1",
            name=job_name(prefix, context.workload_key, context.deployment_name),
            namespace=context.namespace,
            image=context.benchmark_cfg.get("image", DEFAULT_IMAGE),
            timeout=context.benchmark_timeout,
            pvc_size=context.benchmark_cfg.get("pvc_size", "5Gi"),
            guidellm_args=args,
            hf_token_secret=context.benchmark_cfg.get("hf_token_secret", ""),
            fs_group=context.benchmark_cfg.get("fs_group"),
        ).run()

    def run(self, context: BenchmarkContext) -> None:
        workload = context.workload
        rates = workload.get("rates", [1])
        logger.info("Running benchmark at rates=%s for workload=%s", rates, context.workload_key)
        self._run_job(
            context,
            prefix="guidellm-bench",
            rates=rates,
            max_seconds=workload.get("max_seconds", 180),
            rampup=workload.get("rampup"),
        )

    def warmup(self, context: BenchmarkContext) -> None:
        """Prime KV cache and CUDA kernels before the measured run."""
        warmup_cfg = config.project.get_config("rhaiis.warmup", {})
        rate = warmup_cfg.get("rate", 200)
        max_seconds = context.workload.get("warmup", warmup_cfg.get("max_seconds", 60))
        logger.info("Running warmup (concurrency=%d, duration=%ds)", rate, max_seconds)
        try:
            self._run_job(
                context,
                prefix="guidellm-warmup",
                rates=[rate],
                max_seconds=max_seconds,
            )
            logger.info("Warmup completed")
        except Exception:
            logger.warning("Warmup failed; continuing with benchmark", exc_info=True)

    def profile(self, context: BenchmarkContext) -> None:
        """Gate the profiler around each benchmark and collect its traces."""
        from projects.rhaiis.toolbox.copy_profiler_traces.main import run as copy_profiler_traces
        from projects.rhaiis.toolbox.enable_profiler_gate.main import run as enable_profiler_gate
        from projects.rhaiis.toolbox.verify_profiler_prereqs.main import (
            run as verify_profiler_prereqs,
        )

        logger.info("Verifying profiler prerequisites")
        verify_profiler_prereqs(namespace=context.namespace)
        profiler_cfg = runtime_config.get_profiler_config()
        labels = profiler_cfg.get("labels", [])
        if not labels:
            labels = [_profiler_label(context.workload)]
            logger.info("Auto-generated profiler label from workload: %s", labels[0])

        for label_index, label in enumerate(labels):
            logger.info("Profiling label=%s", label)
            enable_profiler_gate(
                name=context.deployment_name,
                namespace=context.namespace,
                gate_value=label if isinstance(label, str) else str(label),
                clear_traces=label_index == 0,
            )
            try:
                self._run_job(
                    context,
                    prefix="guidellm-profiler",
                    rates=profiler_cfg.get("rates", [1]),
                    max_seconds=profiler_cfg.get("max_seconds", 60),
                )
            finally:
                enable_profiler_gate(
                    name=context.deployment_name,
                    namespace=context.namespace,
                    disable=True,
                )

        logger.info("Copying profiler traces from pod")
        copy_profiler_traces(name=context.deployment_name, namespace=context.namespace)
