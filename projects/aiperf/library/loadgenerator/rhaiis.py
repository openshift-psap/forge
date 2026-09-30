"""AI Perf execution adapter for RHAIIS workloads."""

from __future__ import annotations

from projects.aiperf.toolbox.run_aiperf_benchmark import main as aiperf_command
from projects.rhaiis.orchestration import runtime_config
from projects.rhaiis.orchestration.loadgenerator.base import BenchmarkContext, RhaiisLoadGenerator


def _job_name(workload_key: str, deployment_name: str) -> str:
    """Keep the Job name short enough for AI Perf's result PVC suffix."""
    base = f"aiperf-bench-{workload_key}-"
    available = 55 - len(base)
    model = deployment_name[:available] if available > 0 else ""
    return f"{base}{model}".rstrip("-")


class AIPerfGenerator(RhaiisLoadGenerator):
    tool = "aiperf"

    def run(self, context: BenchmarkContext) -> None:
        """Run AI Perf with this workload's profile and tool configuration."""
        cfg = runtime_config.get_aiperf_config()
        workload = context.workload
        model_id = context.model_cfg["hf_model_id"]
        aiperf_command.run(
            endpoint_url=context.endpoint_url,
            model_name=model_id,
            namespace=context.namespace,
            name=_job_name(context.workload_key, context.deployment_name),
            image=cfg["image"],
            timeout=workload.get("timeout_seconds", cfg["timeout"]),
            pvc_size=cfg["pvc_size"],
            pvc_storage_class=cfg.get("pvc_storage_class"),
            tokenizer=workload.get("tokenizer", model_id),
            endpoint_type=workload.get("endpoint_type", "chat"),
            endpoint_path=workload.get("endpoint_path", "/v1/chat/completions"),
            streaming=workload.get("streaming", True),
            concurrency=workload.get("concurrency"),
            request_rate=workload.get("request_rate"),
            request_count=workload.get("request_count"),
            input_tokens=workload.get("input_tokens"),
            output_tokens=workload.get("output_tokens"),
            dataset_url=workload.get("dataset_url"),
            dataset_sha256=workload.get("dataset_sha256"),
            dataset_type=workload.get("dataset_type"),
            fixed_schedule=workload.get("fixed_schedule", False),
            fixed_schedule_auto_offset=workload.get("fixed_schedule_auto_offset", False),
            image_pull_secret=cfg.get("image_pull_secret"),
        )
