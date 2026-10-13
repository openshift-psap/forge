"""ConfigIQ's GuideLLM knee-profile integration."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from projects.core.library import env
from projects.rhaiis.orchestration import runtime_config, test_phase


def _configiq_workload() -> dict:
    path = Path(runtime_config.__file__).parent / "config.d" / "workloads.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["configiq"]


def test_configiq_enables_one_adaptive_knee_run_with_monitoring() -> None:
    workload = _configiq_workload()
    args = runtime_config.build_configiq_knee_args(
        endpoint_url="https://example.test/v1",
        model_id="example/model",
        workload=workload,
    )

    assert workload["guidellm_image"] == "ghcr.io/vllm-project/guidellm:v0.8.1"
    profile = json.loads(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--profile=")))
    assert profile == {
        "kind": "knee",
        "initial_streams": workload["rates"],
        "adaptive": True,
        "points_each_side": 5,
        "max_step": 5,
    }
    constraints = [arg.split("=", 1)[1] for arg in args if arg.startswith("--constraint=")]
    assert "kind=max_duration,seconds=450" in constraints
    assert json.loads(constraints[1]) == {
        "kind": "over_saturation",
        "mode": "monitor",
        **{key: value for key, value in workload["saturation_monitor"].items() if key != "enabled"},
    }
    assert "--output=kind=json,path=/results/benchmarks-default.json" in args


def test_configiq_uses_one_guidellm_job_and_other_workloads_keep_their_runner() -> None:
    configiq = _configiq_workload()
    standard = {"data": "prompt_tokens=32,output_tokens=32", "rates": [1, 5], "max_seconds": 60}
    benchmark_calls = []
    label_calls = []

    def record_benchmark(**kwargs):
        benchmark_calls.append(kwargs)
        return 0

    def record_metadata(_directory, labels):
        label_calls.append(labels)

    with TemporaryDirectory() as directory:
        previous_artifact_dir = env.get_tls_artifact_dir()
        env._set_tls_artifact_dir(Path(directory))
        try:
            with (
                patch.object(
                    test_phase.runtime_config, "get_workload", side_effect=[configiq, standard]
                ),
                patch.object(test_phase, "create_test_metadata", side_effect=record_metadata),
                patch(
                    "projects.guidellm.toolbox.run_guidellm_benchmark.main.run",
                    side_effect=record_benchmark,
                ),
                patch("projects.core.library.config.project") as project_config,
            ):
                project_config.get_config.return_value = True
                for workload_key in ("configiq", "profile1"):
                    test_phase._run_workload_benchmark(
                        model_key="model",
                        workload_key=workload_key,
                        model_cfg={"hf_model_id": "example/model"},
                        accelerator="nvidia",
                        accelerator_key="nvidia",
                        gpu_type="h200",
                        serving_image="example/vllm:test",
                        engine_args={},
                        benchmark_cfg={"image": "ghcr.io/vllm-project/guidellm:v0.7.4"},
                        deployment_name="model",
                        namespace="test",
                        endpoint_url="https://example.test",
                        benchmark_timeout=14400,
                        run_uuid="run-id",
                        version="test",
                        cluster_tag="test",
                    )
        finally:
            env._set_tls_artifact_dir(previous_artifact_dir)

    assert len(benchmark_calls) == 2
    assert [labels["workload_key"] for labels in label_calls] == ["configiq", "profile1"]
    assert benchmark_calls[0]["image"] == "ghcr.io/vllm-project/guidellm:v0.8.1"
    assert benchmark_calls[0]["guidellm_args"] is None
    assert any(arg.startswith("--profile=") for arg in benchmark_calls[0]["guidellm_run_args"])
    assert benchmark_calls[1]["image"] == "ghcr.io/vllm-project/guidellm:v0.7.4"
    assert benchmark_calls[1]["guidellm_run_args"] is None
    assert "--rate=1,5" in benchmark_calls[1]["guidellm_args"]
