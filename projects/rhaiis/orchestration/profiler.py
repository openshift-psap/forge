"""Native (HTTP start/stop) profiler helpers for rhaiis.

Kept in orchestration so toolbox commands stay free of config imports.
The webhook backend is unchanged; this module only prepares deploy-time
args/env and the /start_profile JSON body for the native path.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

NATIVE_ENGINES = ("vllm", "sglang")
DEFAULT_TRACES_DIR = "/tmp/vllm_profile"
DEFAULT_RPC_TIMEOUT_MS = "1800000"


def profiler_backend(profiler_cfg: dict) -> str:
    return str(profiler_cfg.get("backend", "webhook") or "webhook").strip().lower()


def profiler_kind(profiler_cfg: dict) -> str:
    return str(profiler_cfg.get("kind", "torch") or "torch").strip().lower()


def traces_dir(profiler_cfg: dict) -> str:
    return str(profiler_cfg.get("traces_dir") or DEFAULT_TRACES_DIR)


def native_options(profiler_cfg: dict) -> dict:
    return dict(profiler_cfg.get("native") or {})


def is_native_backend(profiler_cfg: dict) -> bool:
    return profiler_backend(profiler_cfg) == "native"


def engine_supports_profiler(engine: str, profiler_cfg: dict) -> bool:
    if is_native_backend(profiler_cfg):
        return engine in NATIVE_ENGINES
    return engine == "vllm"


def build_vllm_profiler_config_json(profiler_cfg: dict) -> str:
    """JSON for vLLM ``--profiler-config`` (v0.13+)."""
    kind = profiler_kind(profiler_cfg)
    if kind not in ("torch", "cuda", "proton"):
        raise ValueError(f"Unsupported rhaiis.profiler.kind: {kind}")

    native = native_options(profiler_cfg)
    cfg: dict = {"profiler": kind}
    directory = traces_dir(profiler_cfg)

    if kind == "torch":
        cfg["torch_profiler_dir"] = directory
        cfg["torch_profiler_with_stack"] = bool(native.get("with_stack", True))
        cfg["torch_profiler_record_shapes"] = bool(native.get("record_shapes", False))
        cfg["torch_profiler_with_memory"] = bool(native.get("with_memory", False))
        cfg["torch_profiler_with_flops"] = bool(native.get("with_flops", False))
        cfg["torch_profiler_use_gzip"] = bool(native.get("use_gzip", True))
        cfg["torch_profiler_dump_cuda_time_total"] = bool(
            native.get("dump_cuda_time_total", True)
        )
        cfg["ignore_frontend"] = bool(native.get("ignore_frontend", True))
        delay = int(native.get("delay_iterations", 0) or 0)
        max_iters = int(native.get("max_iterations", 0) or 0)
        if delay:
            cfg["delay_iterations"] = delay
        if max_iters:
            cfg["max_iterations"] = max_iters
        wait = int(native.get("wait_iterations", 0) or 0)
        warmup = int(native.get("warmup_iterations", 0) or 0)
        active = int(native.get("active_iterations", 5) or 5)
        if wait or warmup:
            cfg["wait_iterations"] = wait
            cfg["warmup_iterations"] = warmup
            cfg["active_iterations"] = active
    elif kind == "proton":
        output_format = str(native.get("proton_output_format") or "chrome_trace")
        cfg["proton_profiler_dir"] = directory
        cfg["proton_output_format"] = output_format
        cfg["proton_data"] = "trace" if output_format == "chrome_trace" else "tree"
        if native.get("proton_hook", "triton"):
            cfg["proton_hook"] = native.get("proton_hook", "triton")
    return json.dumps(cfg, separators=(",", ":"))


def build_sglang_start_body(profiler_cfg: dict) -> str:
    """JSON body for SGLang ``POST /start_profile``."""
    native = native_options(profiler_cfg)
    body: dict = {"output_dir": traces_dir(profiler_cfg)}
    num_steps = native.get("num_steps", native.get("max_iterations", 0))
    num_steps = int(num_steps or 0)
    start_step = int(native.get("start_step", native.get("delay_iterations", 0)) or 0)
    if num_steps > 0:
        body["num_steps"] = num_steps
    if start_step > 0:
        body["start_step"] = start_step
    activities = native.get("activities") or ["CPU", "GPU"]
    if isinstance(activities, str):
        activities = [a.strip() for a in activities.split(",") if a.strip()]
    body["activities"] = activities
    return json.dumps(body, separators=(",", ":"))


def apply_native_profiler_deploy(
    engine: str,
    engine_args: dict,
    env_vars: dict,
    profiler_cfg: dict,
) -> None:
    """Mutate ServingRuntime args/env so native start/stop profiling works.

    vLLM: ``--profiler-config`` plus a long RPC timeout for ``/stop_profile`` flush.
    SGLang: ``SGLANG_TORCH_PROFILER_DIR`` (no extra serve flag required).
    """
    directory = traces_dir(profiler_cfg)
    kind = profiler_kind(profiler_cfg)
    native = native_options(profiler_cfg)

    if engine == "vllm":
        engine_args["profiler-config"] = build_vllm_profiler_config_json(profiler_cfg)
        env_vars.setdefault("VLLM_RPC_TIMEOUT", DEFAULT_RPC_TIMEOUT_MS)
        env_vars.setdefault("VLLM_RPC_GET_DATA_TIMEOUT_MS", DEFAULT_RPC_TIMEOUT_MS)
        if kind == "proton":
            engine_args["enforce-eager"] = True
        if kind == "cuda" and native.get("nsys_wrap", False):
            engine_args["_nsys_wrap"] = True
            engine_args["_nsys_output"] = f"{directory}/nsys_capture"
            logger.info("CUDA profiler: wrapping vLLM process with nsys (image must include nsys)")
    elif engine == "sglang":
        env_vars["SGLANG_TORCH_PROFILER_DIR"] = directory
    else:
        raise ValueError(f"Native profiler does not support engine={engine}")

    logger.info(
        "Native profiler deploy: engine=%s kind=%s traces_dir=%s",
        engine,
        kind,
        directory,
    )
