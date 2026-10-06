"""Shared GuideLLM Job invocation for project load generators."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from projects.guidellm.toolbox.run_guidellm_benchmark import main as benchmark_command

K8S_NAME_MAX = 63


def _format_arg_value(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def build_rate_benchmark_args(
    *,
    benchmark_cfg: dict,
    model_id: str,
    data: str,
    rates: list[int],
    max_seconds: int,
    rampup: int | None = None,
) -> list[str]:
    """Build GuideLLM CLI arguments from a rate-based workload."""
    args = [
        f"--{key.replace('_', '-')}={_format_arg_value(value)}"
        for key, value in benchmark_cfg.get("args", {}).items()
    ]
    args.extend((f"--model={model_id}", f"--data={data}", f"--rate={_format_arg_value(rates)}"))
    args.append(f"--max-seconds={max_seconds}")
    if rampup is not None:
        args.append(f"--rampup={rampup}")
    return args


def job_name(prefix: str, workload_key: str, deployment_name: str) -> str:
    """Build a Kubernetes Job name, trimming the deployment suffix to fit."""
    base = f"{prefix}-{workload_key}-"
    available = K8S_NAME_MAX - len(base)
    model = deployment_name[:available] if available > 0 else ""
    return f"{base}{model}".rstrip("-")


@dataclass(frozen=True)
class GuideLLMJob:
    """Carry one invocation's settings without mutating toolbox task state."""

    endpoint_url: str
    name: str
    namespace: str
    image: str | None = None
    timeout: int | None = None
    pvc_size: str | None = None
    pvc_storage_class: str | None = None
    guidellm_args: list[str] | None = None
    config_path: Path | None = None
    hf_token_secret: str = ""
    fs_group: int | None = None
    use_pvc: bool = False

    def run(self) -> int:
        """Launch GuideLLM through its standalone toolbox command."""
        kwargs = {
            "endpoint_url": self.endpoint_url,
            "name": self.name,
            "namespace": self.namespace,
            "image": self.image,
            "timeout": self.timeout,
            "pvc_size": self.pvc_size,
            "pvc_storage_class": self.pvc_storage_class,
            "guidellm_args": self.guidellm_args,
            "config_path": self.config_path,
            "hf_token_secret": self.hf_token_secret,
            "fs_group": self.fs_group,
            "use_pvc": self.use_pvc,
        }
        return benchmark_command.run(
            **{key: value for key, value in kwargs.items() if value is not None}
        )
