from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import projects.core.library.config as core_config
import projects.core.library.env as env


@pytest.fixture(autouse=True)
def _reset_project_config():
    core_config.project = None
    yield
    core_config.project = None


def _write_yaml(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_init_consolidates_config_chunks_into_top_level_sections(tmp_path: Path) -> None:
    orchestration_dir = tmp_path / "orchestration"
    _write_yaml(
        orchestration_dir / "config.yaml",
        {
            "shared": {"source": "config-file"},
        },
    )
    _write_yaml(
        orchestration_dir / "config.d" / "runtime.yaml",
        {
            "override": "from-chunk",
            "nested": {"chunk_value": 2},
        },
    )
    _write_yaml(
        orchestration_dir / "config.d" / "platform.yaml",
        {"name": "rhoai", "channel": "stable"},
    )

    core_config.init(orchestration_dir, apply_cluster_config=False)

    assert core_config.project.get_config("runtime.override", print=False) == "from-chunk"
    assert core_config.project.get_config("runtime.nested.chunk_value", print=False) == 2
    assert core_config.project.get_config("platform.name", print=False) == "rhoai"
    assert core_config.project.get_config("shared.source", print=False) == "config-file"

    consolidated = yaml.safe_load((env.ARTIFACT_DIR / "config.yaml").read_text())
    assert consolidated["runtime"] == {
        "override": "from-chunk",
        "nested": {"chunk_value": 2},
    }
    assert consolidated["platform"] == {"name": "rhoai", "channel": "stable"}
    assert consolidated["shared"] == {"source": "config-file"}


def test_init_supports_projects_defined_only_with_config_d(tmp_path: Path) -> None:
    orchestration_dir = tmp_path / "orchestration"
    _write_yaml(
        orchestration_dir / "config.d" / "runtime.yaml",
        {"default_preset": "smoke"},
    )
    _write_yaml(
        orchestration_dir / "config.d" / "platform.yaml",
        {"namespace": "test-ns"},
    )

    core_config.init(orchestration_dir, apply_cluster_config=False)

    assert core_config.project.get_config("runtime.default_preset", print=False) == "smoke"
    assert core_config.project.get_config("platform.namespace", print=False) == "test-ns"

    consolidated = yaml.safe_load((env.ARTIFACT_DIR / "config.yaml").read_text())
    assert consolidated["runtime"]["default_preset"] == "smoke"
    assert consolidated["platform"]["namespace"] == "test-ns"


def test_init_rejects_empty_config_d_when_config_yaml_is_missing(tmp_path: Path) -> None:
    orchestration_dir = tmp_path / "orchestration"
    (orchestration_dir / "config.d").mkdir(parents=True)

    with pytest.raises(ValueError, match="config YAML chunks"):
        core_config.init(orchestration_dir, apply_cluster_config=False)


def test_init_rejects_overlapping_sections_between_config_and_config_d(
    tmp_path: Path,
) -> None:
    orchestration_dir = tmp_path / "orchestration"
    _write_yaml(
        orchestration_dir / "config.yaml",
        {"runtime": {"keep": "from-base"}},
    )
    _write_yaml(
        orchestration_dir / "config.d" / "runtime.yaml",
        {"override": "from-chunk"},
    )

    with pytest.raises(ValueError, match="defined in both"):
        core_config.init(orchestration_dir, apply_cluster_config=False)


def test_init_supports_yml_config_chunks(tmp_path: Path) -> None:
    orchestration_dir = tmp_path / "orchestration"
    _write_yaml(
        orchestration_dir / "config.d" / "runtime.yml",
        {"default_preset": "smoke"},
    )

    core_config.init(orchestration_dir, apply_cluster_config=False)

    assert core_config.project.get_config("runtime.default_preset", print=False) == "smoke"


def test_config_chunk_starts_from_shared_defaults(tmp_path: Path, monkeypatch) -> None:
    forge_home = tmp_path / "forge"
    _write_yaml(
        forge_home / "component" / "defaults.yaml",
        {"mode": "off", "collector": {"image": "img:1", "storage_size": "10Gi"}},
    )
    monkeypatch.setattr(env, "FORGE_HOME", forge_home)
    orchestration_dir = tmp_path / "orchestration"
    _write_yaml(
        orchestration_dir / "config.d" / "tracing.yaml",
        {"__defaults": "component/defaults.yaml", "collector": {"storage_size": "20Gi"}},
    )

    core_config.init(orchestration_dir, apply_cluster_config=False)

    consolidated = yaml.safe_load((env.ARTIFACT_DIR / "config.yaml").read_text())
    assert consolidated["tracing"] == {
        "mode": "off",
        "collector": {"image": "img:1", "storage_size": "20Gi"},
    }


def test_config_chunk_rejects_missing_defaults_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(env, "FORGE_HOME", tmp_path)
    orchestration_dir = tmp_path / "orchestration"
    _write_yaml(
        orchestration_dir / "config.d" / "tracing.yaml",
        {"__defaults": "missing/defaults.yaml"},
    )

    with pytest.raises(ValueError, match="__defaults file not found"):
        core_config.init(orchestration_dir, apply_cluster_config=False)
