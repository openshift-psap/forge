"""Tests for the llm_d profiler injector lifecycle wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from projects.core.library import config as core_config
from projects.core.library import env
from projects.llm_d.orchestration import prepare_sequence

PROJECT_ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "orchestration"


@pytest.fixture(autouse=True)
def _reset_project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    env.init()
    core_config.project = None
    yield
    core_config.project = None


def _init_project_config() -> None:
    core_config.init(PROJECT_ORCHESTRATION_DIR, apply_cluster_config=False)


def test_profiler_config_keys_exist() -> None:
    _init_project_config()

    assert core_config.project.get_config("profiler.injector_namespace") == "vllm-profiler"
    assert core_config.project.get_config("profiler.teardown_after_run") is False


def test_ci_post_cleanup_references_a_defined_helper() -> None:
    """Guard against wiring drift: ci.py calls this helper by name."""
    from projects.llm_d.orchestration import ci

    assert callable(prepare_sequence.teardown_profiler_injector_if_configured)
    source = Path(ci.__file__).read_text()
    assert "teardown_profiler_injector_if_configured" in source


def test_deploy_passes_labels_and_injector_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _init_project_config()
    core_config.project.set_config("profiler.injector_namespace", "custom-injector-ns")

    captured = {}

    def fake_deploy(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("projects.kserve.toolbox.deploy_profiler_injector.main.run", fake_deploy)
    prepare_sequence.prepare_profiler_injector("ns-target")

    assert captured["namespace"] == "ns-target"
    assert captured["injector_namespace"] == "custom-injector-ns"
    assert captured["labels"] == prepare_sequence.PROFILER_INJECTOR_LABELS


def test_teardown_skipped_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _init_project_config()

    def fail_teardown(**_kwargs):
        raise AssertionError("teardown must not run when teardown_after_run is false")

    monkeypatch.setattr(
        "projects.kserve.toolbox.teardown_profiler_injector.main.run", fail_teardown
    )
    prepare_sequence.teardown_profiler_injector_if_configured("ns-target")


def test_teardown_runs_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _init_project_config()
    core_config.project.set_config("profiler.teardown_after_run", True)
    core_config.project.set_config("profiler.injector_namespace", "custom-injector-ns")

    captured = {}

    def fake_teardown(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "projects.kserve.toolbox.teardown_profiler_injector.main.run", fake_teardown
    )
    prepare_sequence.teardown_profiler_injector_if_configured("ns-target")

    assert captured["namespace"] == "ns-target"
    assert captured["injector_namespace"] == "custom-injector-ns"
    assert captured["labels"] == prepare_sequence.PROFILER_INJECTOR_LABELS
