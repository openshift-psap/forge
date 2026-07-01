from __future__ import annotations

import logging
import pathlib

from projects.core.library import config, env, run

logger = logging.getLogger(__name__)

CONFIG_DIR = pathlib.Path(__file__).resolve().parent


def init() -> None:
    env.init()
    run.init()
    config.init(CONFIG_DIR)


def get_namespace() -> str:
    return config.project.get_config("rhaiis.namespace")


def get_accelerator() -> str:
    return config.project.get_config("rhaiis.accelerator")


def get_gpu_type(accelerator: str) -> str | None:
    gpu_types = config.project.get_config("rhaiis.gpu_types", None)
    if gpu_types and accelerator in gpu_types:
        return gpu_types[accelerator]
    return None


def get_deploy_config() -> dict:
    return dict(config.project.get_config("rhaiis.deploy"))


def get_benchmark_config() -> dict:
    return dict(config.project.get_config("benchmarks.guidellm"))


def get_vllm_image(accelerator: str) -> str:
    return config.project.get_config(f"rhaiis.images.{accelerator}")


def get_vllm_defaults() -> dict:
    return dict(config.project.get_config("rhaiis.vllm_args"))


def get_model(model_key: str) -> dict:
    return dict(config.project.get_config(f"models.{model_key}"))


def get_workload(workload_key: str) -> dict:
    return dict(config.project.get_config(f"workloads.{workload_key}"))


def get_vaults() -> list[str]:
    return config.project.get_config("vaults")


def get_platform_config() -> dict:
    return dict(config.project.get_config("platform"))


def get_test_model_key() -> str:
    return config.project.get_config("tests.rhaiis.model_key")


def get_test_workload_key() -> str:
    return config.project.get_config("tests.rhaiis.workload_key")


def merge_vllm_args(
    defaults: dict,
    model: dict,
    workload: dict,
) -> dict:
    merged = dict(defaults)
    merged.update(model.get("vllm_args", {}))
    merged.update(workload.get("vllm_args", {}))
    return merged


def merge_env_vars(accelerator: str, model: dict) -> dict:
    base = dict(config.project.get_config("rhaiis.env_vars") or {})
    base.update(model.get("env_vars", {}))
    accel_vars = config.project.get_config(f"rhaiis.accelerator_env_vars.{accelerator}") or {}
    base.update(accel_vars)
    return base


def _format_arg_value(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def build_guidellm_args(
    *,
    benchmark_cfg: dict,
    model_id: str,
    data: str,
    rates: list[int],
    max_seconds: int,
) -> list[str]:
    guidellm_args = []
    for key, value in benchmark_cfg.get("args", {}).items():
        cli_key = key.replace("_", "-")
        guidellm_args.append(f"--{cli_key}={_format_arg_value(value)}")

    guidellm_args.append(f"--model={model_id}")
    guidellm_args.append(f"--data={data}")
    guidellm_args.append(f"--rate={_format_arg_value(rates)}")
    guidellm_args.append(f"--max-seconds={max_seconds}")
    return guidellm_args


def split_image_tag(full_image: str) -> tuple[str, str]:
    if ":" in full_image:
        parts = full_image.rsplit(":", 1)
        return parts[0], parts[1]
    return full_image, "latest"


def derive_deployment_name(hf_model_id: str) -> str:
    parts = hf_model_id.split("/")
    return (parts[1] if len(parts) > 1 else hf_model_id).lower().replace(".", "-")
