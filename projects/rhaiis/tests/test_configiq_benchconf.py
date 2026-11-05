from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from projects.guidellm.toolbox.run_guidellm_benchmark import main as guidellm_runner
from projects.guidellm.toolbox.run_guidellm_benchmark.utils import _build_multi_run_script
from projects.rhaiis.orchestration import runtime_config, test_phase


def test_configiq_workloads_point_to_benchconf() -> None:
    config_path = Path(__file__).resolve().parents[1] / "orchestration/config.d/workloads.yaml"
    workloads = yaml.safe_load(config_path.read_text())

    for name in (
        "interactive-chat",
        "reasoning",
        "code-generation",
        "deep-research",
        "batch-summarization-rag",
        "batch-synthetic-data-generation",
    ):
        assert workloads[f"configiq-{name}"] == {"benchconf": f"configiq/{name}"}


def test_load_configiq_workload_uses_knee_settings(monkeypatch, tmp_path) -> None:
    scenario_path = tmp_path / "code-generation.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "spec": {
                    "profile": {
                        "kind": "knee",
                        "initial_streams": [1, 2, 5],
                        "warmup": 75,
                        "rampup_duration": 35,
                    },
                    "constraints": [{"kind": "max_duration", "seconds": 21600}],
                }
            }
        )
    )
    monkeypatch.setattr(
        runtime_config,
        "get_workload",
        lambda key: {"benchconf": "configiq/code-generation"},
    )
    monkeypatch.setattr(test_phase.benchconf_lib, "_is_enabled", lambda: True)
    monkeypatch.setattr(test_phase.benchconf_lib, "maybe_install_custom_version", lambda: None)
    monkeypatch.setattr(test_phase.benchconf_lib, "save_version", lambda: None)
    monkeypatch.setattr(
        test_phase.benchconf_lib,
        "resolve_config_path",
        lambda ref: scenario_path,
    )

    workload = test_phase._load_workloads(["configiq-code-generation"])["configiq-code-generation"]

    assert workload["config_path"] == scenario_path
    assert workload["rates"] == [1, 2, 5]
    assert workload["max_seconds"] == 21600
    assert workload["timeout_seconds"] == 3 * (21600 + 75 + 35) + 600


def test_runner_preserves_scenario_profile() -> None:
    script = _build_multi_run_script(
        endpoint_url="http://model.example/v1",
        runs=guidellm_runner.expand_guidellm_runs(["--model=org/model"]),
        config_content="spec:\n  profile:\n    kind: knee\n",
    )

    assert "--config=/tmp/guidellm-config.yaml" in script
    assert "--profile=" not in script
    assert "--backend=kind=openai_http,target=http://model.example/v1,model=org/model" in script


def test_configiq_benchmark_passes_scenario_without_workload_overrides(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(test_phase.env, "NextArtifactDir", lambda name: nullcontext())
    monkeypatch.setattr(test_phase, "_create_test_labels", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        test_phase.runtime_config.config,
        "project",
        SimpleNamespace(get_config=lambda key, default=None: default),
    )
    monkeypatch.setattr(guidellm_runner, "run", lambda **kwargs: captured.update(kwargs))

    test_phase._run_workload_benchmark(
        model_key="model",
        workload_key="configiq-interactive-chat",
        workload={
            "config_path": Path("/benchconf/configiq/interactive-chat.yaml"),
            "rates": [1, 2],
            "max_seconds": 1200,
            "timeout_seconds": 15000,
        },
        model_cfg={"hf_model_id": "org/model"},
        accelerator="nvidia",
        accelerator_key="H200",
        gpu_type="h200",
        serving_image="vllm:test",
        engine_args={},
        env_vars={},
        benchmark_cfg={
            "args": {
                "backend_type": "openai_http",
                "rate_type": "concurrent",
                "request_type": "/v1/completions",
            }
        },
        deployment_name="model",
        namespace="test",
        endpoint_url="http://model.example",
        benchmark_timeout=14400,
        run_uuid="run-id",
        version="test",
        cluster_tag="test",
    )

    assert captured["config_path"] == Path("/benchconf/configiq/interactive-chat.yaml")
    assert captured["timeout"] == 15000
    assert captured["guidellm_args"] == [
        "--backend-type=openai_http",
        "--rate-type=concurrent",
        "--model=org/model",
    ]


def test_disabled_benchconf_fails_for_configiq(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_config,
        "get_workload",
        lambda key: {"benchconf": "configiq/interactive-chat"},
    )
    monkeypatch.setattr(test_phase.benchconf_lib, "_is_enabled", lambda: False)

    with pytest.raises(RuntimeError, match="BenchConf but it is disabled"):
        test_phase._load_workloads(["configiq-interactive-chat"])
