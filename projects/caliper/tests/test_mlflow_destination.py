"""Tests for job-level MLflow destination persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.constants import METADATA_FILE, MLFLOW_DESTINATION_FILE
from projects.caliper.engine.file_export.artifacts_export_run import discover_run_dirs
from projects.caliper.engine.kpi.dataclasses import MlflowDestination
from projects.caliper.engine.traverse import discover_test_bases
from projects.caliper.orchestration import export
from projects.caliper.orchestration.export import (
    ensure_mlflow_destination_marker,
    read_mlflow_destination_marker,
    write_mlflow_destination_marker,
)


def _configure_fournos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable Fournos behavior for marker persistence tests."""
    monkeypatch.setenv("FOURNOS_CI", "true")


def _write_metadata(directory: Path, mlflow_destination: dict[str, str]) -> None:
    """Write a minimal metadata fixture without depending on the metadata writer API."""
    directory.mkdir()
    (directory / METADATA_FILE).write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "labels": {},
                "mlflow_destination": mlflow_destination,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_job_marker_is_discoverable_without_becoming_a_benchmark_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The root marker is read without being treated as a benchmark directory."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    destination = MlflowDestination(run_id="run-123", experiment_id="264", workspace="forge-rhaiis")

    marker = write_mlflow_destination_marker(destination)
    nested_marker = tmp_path / "001__benchmark" / MLFLOW_DESTINATION_FILE
    nested_marker.parent.mkdir()
    nested_marker.write_text(
        yaml.safe_dump({"run_id": "child-run", "experiment_id": "265"}),
        encoding="utf-8",
    )

    assert marker == tmp_path / MLFLOW_DESTINATION_FILE
    assert yaml.safe_load(marker.read_text(encoding="utf-8")) == destination.to_dict()
    assert read_mlflow_destination_marker().to_dict() == destination.to_dict()
    assert discover_run_dirs(tmp_path) == []
    nodes, _excluded = discover_test_bases(tmp_path)
    assert nodes == []


def test_job_marker_is_the_only_export_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The root job marker remains authoritative when test metadata also has a value."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    job_destination = MlflowDestination(
        run_id="job-run", experiment_id="264", workspace="forge-rhaiis"
    )
    write_mlflow_destination_marker(job_destination)

    benchmark_dir = tmp_path / "001__benchmark"
    _write_metadata(
        benchmark_dir,
        {"run_id": "child-run", "experiment_id": "264"},
    )

    assert read_mlflow_destination_marker().to_dict() == job_destination.to_dict()
    assert discover_run_dirs(tmp_path) == [benchmark_dir]
    nodes, _excluded = discover_test_bases(tmp_path)
    assert [node.directory for node in nodes] == [benchmark_dir]


def test_marker_reader_uses_the_shared_artifact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Failure notification lookup reads only the shared job marker."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    write_mlflow_destination_marker(MlflowDestination(run_id="job-run", experiment_id="264"))
    broken_child_metadata = tmp_path / "001__benchmark" / METADATA_FILE
    broken_child_metadata.parent.mkdir()
    broken_child_metadata.write_text("not: [valid", encoding="utf-8")

    assert read_mlflow_destination_marker().to_dict() == {
        "run_id": "job-run",
        "experiment_id": "264",
        "workspace": "",
    }


def test_existing_job_marker_cannot_be_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A job marker can only be created once and is never overwritten."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    destination = MlflowDestination(run_id="run-123", experiment_id="264")

    first = write_mlflow_destination_marker(destination)

    assert first == tmp_path / MLFLOW_DESTINATION_FILE
    with pytest.raises(FileExistsError):
        write_mlflow_destination_marker(destination)
    with pytest.raises(FileExistsError):
        write_mlflow_destination_marker(MlflowDestination(run_id="different", experiment_id="264"))
    assert yaml.safe_load(first.read_text(encoding="utf-8")) == destination.to_dict()


def test_ensure_marker_does_not_recreate_existing_job_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Phase entrypoints reuse an existing job marker without creating another run."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    destination = MlflowDestination(run_id="run-123", experiment_id="264")
    marker = write_mlflow_destination_marker(destination)

    monkeypatch.setattr(
        "projects.caliper.orchestration.export.precreate_mlflow_run_if_configured",
        lambda: pytest.fail("existing job marker must not pre-create another run"),
    )

    assert ensure_mlflow_destination_marker() == marker
    assert read_mlflow_destination_marker().to_dict() == destination.to_dict()


def test_ensure_marker_persists_a_new_job_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The first phase creates the job marker from the pre-created destination."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    destination = MlflowDestination(run_id="run-123", experiment_id="264")
    monkeypatch.setattr(
        "projects.caliper.orchestration.export.precreate_mlflow_run_if_configured",
        lambda: destination,
    )

    marker = ensure_mlflow_destination_marker()

    assert marker == tmp_path / MLFLOW_DESTINATION_FILE
    assert yaml.safe_load(marker.read_text(encoding="utf-8")) == destination.to_dict()


def test_existing_job_marker_is_validated_before_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Malformed or incomplete existing markers fail during phase setup."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    marker = tmp_path / MLFLOW_DESTINATION_FILE

    marker.write_text("not: [valid", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        ensure_mlflow_destination_marker()

    marker.write_text(yaml.safe_dump({"run_id": "run-123"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Incomplete MLflow destination"):
        ensure_mlflow_destination_marker()


def test_marker_is_not_written_outside_fournos_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Marker creation is disabled outside Fournos CI."""
    monkeypatch.setenv("FOURNOS_CI", "false")

    assert (
        write_mlflow_destination_marker(MlflowDestination(run_id="run-123", experiment_id="264"))
        is None
    )
    assert not (tmp_path / MLFLOW_DESTINATION_FILE).exists()


def test_invalid_job_marker_is_not_silently_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Malformed marker YAML remains visible to callers as an exception."""
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    marker = tmp_path / MLFLOW_DESTINATION_FILE
    marker.write_text("not: [valid", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        read_mlflow_destination_marker()


def test_multi_run_export_reuses_job_marker_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The multi-run orchestration path passes the job run ID to the backend."""
    _configure_fournos(monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(artifact_root))
    write_mlflow_destination_marker(MlflowDestination(run_id="job-run", experiment_id="264"))

    secrets_path = tmp_path / "mlflow-secret.yaml"
    secrets_path.write_text("tracking_uri: https://mlflow.example\n", encoding="utf-8")
    monkeypatch.setattr(export.vault_lib, "get_vault_content_path", lambda *_args: secrets_path)
    monkeypatch.setattr(export.env, "ARTIFACT_DIR", tmp_path / "export-step", raising=False)
    monkeypatch.setattr(
        export,
        "discover_run_dirs",
        lambda _from_path: [artifact_root / "run-a", artifact_root / "run-b"],
    )

    captured: dict[str, object] = {}

    def fake_multi_run_export(**kwargs):
        captured.update(kwargs)
        status_yaml = kwargs["status_yaml"]
        status_yaml.parent.mkdir(parents=True, exist_ok=True)
        status_yaml.write_text("success: true\nfinal_status: success\n", encoding="utf-8")

    monkeypatch.setattr(export, "_run_multi_run_export", fake_multi_run_export)

    export.run_from_orchestration_config(
        {
            "export": {
                "from": str(artifact_root),
                "backend": {
                    "mlflow": {
                        "enabled": True,
                        "secrets": {"vault": {"name": "mlflow", "mlflow_secret": "secret"}},
                        "config": {"workspace": "forge", "experiment": "forge"},
                    }
                },
            }
        }
    )

    assert captured["mlflow_run_id"] == "job-run"
