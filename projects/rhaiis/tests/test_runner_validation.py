from pathlib import Path

import pytest

from projects.core.library import config, env
from projects.rhaiis.orchestration import test_phase

ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "orchestration"


@pytest.fixture(autouse=True)
def _project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    env.init()
    config.project = None
    config.init(ORCHESTRATION_DIR, apply_cluster_config=False)
    config.project.config["workloads"]["aiperf-review"] = {"tool": "aiperf"}
    yield
    config.project = None


def test_unavailable_runner_fails_before_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    from projects.rhaiis.toolbox.deploy_kserve_isvc import main as deploy_kserve_isvc

    monkeypatch.setattr(
        deploy_kserve_isvc,
        "run",
        lambda **_kwargs: pytest.fail("deployment started before runner validation"),
    )

    with pytest.raises(ValueError, match="Benchmark tool 'aiperf' has no runner"):
        test_phase._run_test(
            model_key="qwen3-0_6b",
            workload_keys=["aiperf-review"],
            namespace="kserve-e2e-perf",
        )


def test_standalone_analysis_accepts_selected_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from projects.rhaiis.orchestration import analysis

    config.project.set_config("tests.rhaiis.run_benchmark", False, print=False)
    monkeypatch.setattr(analysis, "run_standalone_analysis", lambda *_args, **_kwargs: None)

    assert (
        test_phase._run_test(
            model_key="qwen3-0_6b",
            workload_keys=["aiperf-review"],
            namespace="kserve-e2e-perf",
        )
        == 0
    )
