from __future__ import annotations

import json
from pathlib import Path

from projects.rhaiis.orchestration.profiler import (
    apply_native_profiler_deploy,
    build_sglang_start_body,
    build_vllm_profiler_config_json,
    engine_supports_profiler,
    is_native_backend,
)
from projects.rhaiis.postprocess.s3_dashboard import _is_uploadable_trace
from projects.rhaiis.toolbox.copy_profiler_traces.main import normalize_trace_name


def test_webhook_is_default_backend() -> None:
    assert is_native_backend({}) is False
    assert is_native_backend({"backend": "webhook"}) is False
    assert is_native_backend({"backend": "native"}) is True


def test_engine_support_matrix() -> None:
    webhook = {"backend": "webhook"}
    native = {"backend": "native"}
    assert engine_supports_profiler("vllm", webhook)
    assert not engine_supports_profiler("sglang", webhook)
    assert engine_supports_profiler("vllm", native)
    assert engine_supports_profiler("sglang", native)
    assert not engine_supports_profiler("trtllm", native)


def test_vllm_torch_profiler_config_json() -> None:
    cfg = {
        "kind": "torch",
        "traces_dir": "/tmp/vllm_profile",
        "native": {"with_stack": True, "ignore_frontend": True},
    }
    parsed = json.loads(build_vllm_profiler_config_json(cfg))
    assert parsed["profiler"] == "torch"
    assert parsed["torch_profiler_dir"] == "/tmp/vllm_profile"
    assert parsed["ignore_frontend"] is True
    assert "wait_iterations" not in parsed
    assert "max_iterations" not in parsed


def test_vllm_torch_schedule_included_when_warmup_set() -> None:
    cfg = {
        "kind": "torch",
        "native": {"warmup_iterations": 2, "active_iterations": 8, "wait_iterations": 1},
    }
    parsed = json.loads(build_vllm_profiler_config_json(cfg))
    assert parsed["warmup_iterations"] == 2
    assert parsed["active_iterations"] == 8
    assert parsed["wait_iterations"] == 1


def test_vllm_cuda_and_proton_kinds() -> None:
    cuda = json.loads(build_vllm_profiler_config_json({"kind": "cuda"}))
    assert cuda == {"profiler": "cuda"}

    proton = json.loads(
        build_vllm_profiler_config_json(
            {"kind": "proton", "traces_dir": "/tmp/vllm_profile"}
        )
    )
    assert proton["profiler"] == "proton"
    assert proton["proton_profiler_dir"] == "/tmp/vllm_profile"
    assert proton["proton_data"] == "trace"


def test_apply_native_vllm_sets_profiler_config() -> None:
    args: dict = {}
    env: dict = {}
    apply_native_profiler_deploy("vllm", args, env, {"backend": "native", "kind": "torch"})
    assert "profiler-config" in args
    assert env["VLLM_RPC_TIMEOUT"] == "1800000"


def test_apply_native_cuda_nsys_wrap() -> None:
    args: dict = {}
    env: dict = {}
    apply_native_profiler_deploy(
        "vllm",
        args,
        env,
        {"backend": "native", "kind": "cuda", "native": {"nsys_wrap": True}},
    )
    assert args["_nsys_wrap"] is True
    assert args["_nsys_output"].endswith("/nsys_capture")


def test_apply_native_sglang_sets_env_only() -> None:
    args: dict = {"tp-size": 2}
    env: dict = {}
    apply_native_profiler_deploy("sglang", args, env, {"backend": "native"})
    assert "profiler-config" not in args
    assert env["SGLANG_TORCH_PROFILER_DIR"] == "/tmp/vllm_profile"


def test_sglang_start_body() -> None:
    body = json.loads(
        build_sglang_start_body({"native": {"num_steps": 10, "start_step": 5}})
    )
    assert body["num_steps"] == 10
    assert body["start_step"] == 5
    assert body["output_dir"] == "/tmp/vllm_profile"


def test_normalize_trace_name_prefixes_native_files() -> None:
    name = normalize_trace_name("worker.pt.trace.json.gz", "isl1000_osl1000")
    assert name.startswith("trace_")
    assert "rank0" in name
    assert "isl1000_osl1000" in name
    assert name.endswith(".pt.trace.json.gz")


def test_uploadable_trace_accepts_gzip_and_proton() -> None:
    assert _is_uploadable_trace(Path("trace_rank0_runisl_x.pt.trace.json.gz"))
    assert _is_uploadable_trace(Path("trace_rank0_runisl.hatchet"))
    assert not _is_uploadable_trace(Path("trace_rank1.json"))
    assert not _is_uploadable_trace(Path("notes.txt"))
