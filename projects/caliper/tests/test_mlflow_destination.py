"""Tests for job-level MLflow destination persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.constants import METADATA_FILE, MLFLOW_DESTINATION_FILE
from projects.caliper.engine.file_export.artifacts_export_run import discover_run_dirs
from projects.caliper.engine.traverse import discover_test_bases
from projects.caliper.orchestration.export import (
    _discover_precreated_mlflow_run_id,
    _read_mlflow_destinations,
    _read_mlflow_ids_from_test_labels,
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
    assert _read_mlflow_destinations(tmp_path) == [destination]
    assert _discover_precreated_mlflow_run_id(tmp_path) == "run-123"
    assert discover_run_dirs(tmp_path) == []
    nodes, _excluded = discover_test_bases(tmp_path)
    assert nodes == []


def test_job_marker_takes_precedence_over_benchmark_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The root job marker takes precedence over benchmark metadata destinations."""
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

    assert _discover_precreated_mlflow_run_id(tmp_path) == "job-run"
    assert _read_mlflow_destinations(tmp_path) == [
        job_destination,
        {"run_id": "child-run", "experiment_id": "264"},
    ]
    assert discover_run_dirs(tmp_path) == [benchmark_dir]
    nodes, _excluded = discover_test_bases(tmp_path)
    assert [node.directory for node in nodes] == [benchmark_dir]


def test_url_reader_uses_the_shared_artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Failure notification lookup reads the marker from the shared artifact root."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    write_mlflow_destination_marker(
        {"run_id": "job-run", "experiment_id": "264"}, artifact_root=tmp_path
    )
    broken_child_metadata = tmp_path / "001__benchmark" / METADATA_FILE
    broken_child_metadata.parent.mkdir()
    broken_child_metadata.write_text("not: [valid", encoding="utf-8")

    assert _read_mlflow_ids_from_test_labels() == ("job-run", "264")
    with pytest.raises(yaml.YAMLError):
        _read_mlflow_destinations(tmp_path)


def test_run_id_only_metadata_still_resumes_the_precreated_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Legacy metadata containing only run_id remains usable for export resumption."""
    _configure_fournos(monkeypatch)
    monkeypatch.setenv("ARTIFACT_BASE_DIR", str(tmp_path))
    benchmark_dir = tmp_path / "001__benchmark"
    _write_metadata(benchmark_dir, {"run_id": "legacy-run"})

    assert _discover_precreated_mlflow_run_id(tmp_path) == "legacy-run"
    assert _read_mlflow_ids_from_test_labels() == ("legacy-run", "")


def test_same_job_marker_is_idempotent_but_conflicts_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Identical writes reuse the marker while conflicting writes fail."""
    _configure_fournos(monkeypatch)
    destination = {"run_id": "run-123", "experiment_id": "264"}

    first = write_mlflow_destination_marker(destination, artifact_root=tmp_path)
    second = write_mlflow_destination_marker(destination, artifact_root=tmp_path)

    assert second == first
    assert not list(tmp_path.glob(f".{MLFLOW_DESTINATION_FILE}.*"))
    with pytest.raises(RuntimeError, match="Conflicting MLflow destination marker"):
        write_mlflow_destination_marker(
            {"run_id": "different", "experiment_id": "264"}, artifact_root=tmp_path
        )


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
        _read_mlflow_destinations(tmp_path)
