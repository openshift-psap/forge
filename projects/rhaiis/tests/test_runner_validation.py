from dataclasses import replace
from pathlib import Path

import pytest

from projects.core.library import config, env
from projects.guidellm.library import runner as guidellm_runner
from projects.guidellm.toolbox.run_guidellm_benchmark.main import wait_guidellm_benchmark_task
from projects.rhaiis.orchestration import loadgenerator, test_phase

ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "orchestration"


@pytest.fixture(autouse=True)
def _project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    env.init()
    config.project = None
    config.init(ORCHESTRATION_DIR, apply_cluster_config=False)
    config.project.config["workloads"]["runner-review"] = {"tool": "guidellm"}
    yield
    config.project = None


def test_unavailable_runner_fails_before_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    from projects.rhaiis.toolbox.deploy_kserve_isvc import main as deploy_kserve_isvc

    monkeypatch.setattr(loadgenerator, "RUNNERS", {})
    monkeypatch.setattr(
        deploy_kserve_isvc,
        "run",
        lambda **_kwargs: pytest.fail("deployment started before runner validation"),
    )

    with pytest.raises(ValueError, match="Benchmark tool 'guidellm' has no runner"):
        test_phase._run_test(
            model_key="qwen3-0_6b",
            workload_keys=["runner-review"],
            namespace="kserve-e2e-perf",
        )


def test_standalone_analysis_accepts_selected_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from projects.rhaiis.orchestration import analysis

    config.project.set_config("tests.rhaiis.run_benchmark", False, print=False)
    monkeypatch.setattr(loadgenerator, "RUNNERS", {})
    monkeypatch.setattr(analysis, "run_standalone_analysis", lambda *_args, **_kwargs: None)

    assert (
        test_phase._run_test(
            model_key="qwen3-0_6b",
            workload_keys=["runner-review"],
            namespace="kserve-e2e-perf",
        )
        == 0
    )


def test_guidellm_runner_preserves_benchmark_job_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    retry_settings = wait_guidellm_benchmark_task._retry_config.copy()
    monkeypatch.setattr(
        guidellm_runner.benchmark_command, "run", lambda **kwargs: calls.append(kwargs)
    )
    context = loadgenerator.BenchmarkContext(
        deployment_name="qwen-server",
        namespace="test-ns",
        endpoint_url="http://qwen-server.test-ns:8080",
        benchmark_cfg={"image": "guidellm:test", "pvc_size": "10Gi", "args": {}},
        model_cfg={"hf_model_id": "Qwen/Qwen3-0.6B"},
        workload={"data": "prompt_tokens=100,output_tokens=50", "rates": [1, 4], "max_seconds": 30},
        workload_key="profile1",
        benchmark_timeout=600,
    )

    first_generator = loadgenerator.get_load_generator("guidellm")
    second_generator = loadgenerator.get_load_generator("guidellm")
    assert first_generator is not second_generator
    first_generator.run(context)
    second_generator.run(replace(context, benchmark_timeout=1200))

    assert len(calls) == 2
    assert calls[0]["endpoint_url"] == "http://qwen-server.test-ns:8080/v1"
    assert calls[0]["name"] == "guidellm-bench-profile1-qwen-server"
    assert calls[0]["image"] == "guidellm:test"
    assert calls[0]["timeout"] == 600
    assert calls[1]["timeout"] == 1200
    assert "--rate=1,4" in calls[0]["guidellm_args"]
    assert "--max-seconds=30" in calls[0]["guidellm_args"]
    assert wait_guidellm_benchmark_task._retry_config == retry_settings
