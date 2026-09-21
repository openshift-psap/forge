"""Tests for job-level MLflow destination persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.constants import METADATA_FILE, MLFLOW_DESTINATION_FILE
from projects.caliper.engine.file_export.artifacts_export_run import discover_run_dirs
from projects.caliper.engine.traverse import discover_test_bases
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
    destination = {
        "run_id": "run-123",
        "experiment_id": "264",
        "workspace": "forge-rhaiis",
    }

    marker = write_mlflow_destination_marker(destination, artifact_root=tmp_path)
    nested_marker = tmp_path / "001__benchmark" / MLFLOW_DESTINATION_FILE
    nested_marker.parent.mkdir()
    nested_marker.write_text(
        yaml.safe_dump({"run_id": "child-run", "experiment_id": "265"}),
        encoding="utf-8",
    )

    assert marker == tmp_path / MLFLOW_DESTINATION_FILE
    assert yaml.safe_load(marker.read_text(encoding="utf-8")) == destination
    assert read_mlflow_destination_marker(tmp_path) == destination
    assert discover_run_dirs(tmp_path) == []
    nodes, _excluded = discover_test_bases(tmp_path)
    assert nodes == []


def test_job_marker_is_the_only_export_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The root job marker remains authoritative when test metadata also has a value."""
    _configure_fournos(monkeypatch)
    job_destination = {
        "run_id": "job-run",
        "experiment_id": "264",
        "workspace": "forge-rhaiis",
    }
    write_mlflow_destination_marker(job_destination, artifact_root=tmp_path)

    benchmark_dir = tmp_path / "001__benchmark"
    _write_metadata(
        benchmark_dir,
        {"run_id": "child-run", "experiment_id": "264"},
    )

    assert read_mlflow_destination_marker(tmp_path) == job_destination
    assert discover_run_dirs(tmp_path) == [benchmark_dir]
    nodes, _excluded = discover_test_bases(tmp_path)
    assert [node.directory for node in nodes] == [benchmark_dir]


def test_marker_reader_uses_the_shared_artifact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Failure notification lookup reads only the shared job marker."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    write_mlflow_destination_marker(
        {"run_id": "job-run", "experiment_id": "264"}, artifact_root=tmp_path
    )
    broken_child_metadata = tmp_path / "001__benchmark" / METADATA_FILE
    broken_child_metadata.parent.mkdir()
    broken_child_metadata.write_text("not: [valid", encoding="utf-8")

    assert read_mlflow_destination_marker() == {"run_id": "job-run", "experiment_id": "264"}


def test_existing_job_marker_cannot_be_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A job marker can only be created once and is never overwritten."""
    _configure_fournos(monkeypatch)
    destination = {"run_id": "run-123", "experiment_id": "264"}

    first = write_mlflow_destination_marker(destination, artifact_root=tmp_path)

    assert first == tmp_path / MLFLOW_DESTINATION_FILE
    with pytest.raises(FileExistsError):
        write_mlflow_destination_marker(destination, artifact_root=tmp_path)
    with pytest.raises(FileExistsError):
        write_mlflow_destination_marker(
            {"run_id": "different", "experiment_id": "264"}, artifact_root=tmp_path
        )
    assert yaml.safe_load(first.read_text(encoding="utf-8")) == destination


def test_ensure_marker_does_not_recreate_existing_job_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Phase entrypoints reuse an existing job marker without creating another run."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    destination = {"run_id": "run-123", "experiment_id": "264"}
    marker = write_mlflow_destination_marker(destination, artifact_root=tmp_path)

    monkeypatch.setattr(
        "projects.caliper.orchestration.export.precreate_mlflow_run_if_configured",
        lambda: pytest.fail("existing job marker must not pre-create another run"),
    )

    assert ensure_mlflow_destination_marker() == marker
    assert read_mlflow_destination_marker() == destination


def test_ensure_marker_persists_a_new_job_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The first phase creates the job marker from the pre-created destination."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    destination = {"run_id": "run-123", "experiment_id": "264"}
    monkeypatch.setattr(
        "projects.caliper.orchestration.export.precreate_mlflow_run_if_configured",
        lambda: destination,
    )

    marker = ensure_mlflow_destination_marker()

    assert marker == tmp_path / MLFLOW_DESTINATION_FILE
    assert yaml.safe_load(marker.read_text(encoding="utf-8")) == destination


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
        write_mlflow_destination_marker(
            {"run_id": "run-123", "experiment_id": "264"}, artifact_root=tmp_path
        )
        is None
    )
    assert not (tmp_path / MLFLOW_DESTINATION_FILE).exists()


def test_invalid_job_marker_is_not_silently_ignored(tmp_path: Path):
    """Malformed marker YAML remains visible to callers as an exception."""
    marker = tmp_path / MLFLOW_DESTINATION_FILE
    marker.write_text("not: [valid", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        read_mlflow_destination_marker(tmp_path)
