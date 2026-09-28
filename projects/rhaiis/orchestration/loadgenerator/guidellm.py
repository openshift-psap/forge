"""GuideLLM execution, warmup, and profiler steps for RHAIIS."""

from __future__ import annotations

import logging

from projects.core.library import config
from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
    run as run_guidellm_benchmark,
)
from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
    wait_guidellm_benchmark_task,
)
from projects.rhaiis.orchestration import runtime_config

from .base import BenchmarkContext, RhaiisLoadGenerator

logger = logging.getLogger(__name__)
_K8S_NAME_MAX = 63
_DEFAULT_IMAGE = "ghcr.io/vllm-project/guidellm:v0.7.4"


def _format_arg_value(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def build_guidellm_args(
    *,
    benchmark_cfg: dict,
    model_id: str,
    data: str,
    rates: list[int],
    max_seconds: int,
    rampup: int | None = None,
) -> list[str]:
    args = [
        f"--{key.replace('_', '-')}={_format_arg_value(value)}"
        for key, value in benchmark_cfg.get("args", {}).items()
    ]
    args.extend((f"--model={model_id}", f"--data={data}", f"--rate={_format_arg_value(rates)}"))
    args.append(f"--max-seconds={max_seconds}")
    if rampup is not None:
        args.append(f"--rampup={rampup}")
    return args


def _job_name(prefix: str, workload_key: str, deployment_name: str) -> str:
    base = f"{prefix}-{workload_key}-"
    available = _K8S_NAME_MAX - len(base)
    model = deployment_name[:available] if available > 0 else ""
    return f"{base}{model}".rstrip("-")


def _profiler_label(workload: dict) -> str:
    data = workload.get("data", "")
    params = dict(item.split("=", 1) for item in data.split(",") if "=" in item)
    return f"isl{params.get('prompt_tokens', '0')}_osl{params.get('output_tokens', '0')}"


class GuideLLMGenerator(RhaiisLoadGenerator):
    tool = "guidellm"

    def configure_timeout(self, timeout: int) -> None:
        wait_guidellm_benchmark_task._retry_config["attempts"] = max(1, timeout // 10)

    def _run_job(
        self,
        context: BenchmarkContext,
        *,
        prefix: str,
        rates: list[int],
        max_seconds: int,
        rampup: int | None = None,
    ) -> None:
        args = build_guidellm_args(
            benchmark_cfg=context.benchmark_cfg,
            model_id=context.model_cfg["hf_model_id"],
            data=context.workload["data"],
            rates=rates,
            max_seconds=max_seconds,
            rampup=rampup,
        )
        run_guidellm_benchmark(
            endpoint_url=f"{context.endpoint_url}/v1",
            name=_job_name(prefix, context.workload_key, context.deployment_name),
            namespace=context.namespace,
            image=context.benchmark_cfg.get("image", _DEFAULT_IMAGE),
            timeout=context.benchmark_timeout,
            pvc_size=context.benchmark_cfg.get("pvc_size", "5Gi"),
            guidellm_args=args,
            hf_token_secret=context.benchmark_cfg.get("hf_token_secret", ""),
            fs_group=context.benchmark_cfg.get("fs_group"),
        )

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

        for label in labels:
            logger.info("Profiling label=%s", label)
            enable_profiler_gate(
                name=context.deployment_name,
                namespace=context.namespace,
                gate_value=label if isinstance(label, str) else str(label),
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
        try:
            copy_profiler_traces(name=context.deployment_name, namespace=context.namespace)
        except Exception:
            logger.warning("Failed to copy profiler traces", exc_info=True)
