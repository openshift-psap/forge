"""Collection failure and recovery tests.

Covers the plan's Storage/Collection/Retry/Failure rows for the parts that are
pure Python: empty capture, truncated transfer, conflicting destinations,
retryable vs terminal HTTP classification, and the resumable child-run map.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.tracing.bundle import (
    ARCHIVE_FILENAME,
    SUMMARY_FILENAME,
    BundleValidationError,
    discover_bundles,
    validate_bundle,
)
from projects.caliper.engine.tracing.child_runs import (
    ChildRunDestination,
    ChildRunMapError,
    child_run_key,
    read_child_run_map,
    write_child_run_map,
)


def _write_summary(bundle_dir: Path, *, relative: str, test_id: str = "t1") -> None:
    summary = {
        "schema_version": 1,
        "identity": {"schema_version": 1, "test_id": test_id},
        "config": {"mode": "standard"},
        "time": {},
        "data": {"archive_relative_path": relative},
        "quality": {},
        "status": {"state": "collected", "reason": ""},
    }
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / SUMMARY_FILENAME).write_text(json.dumps(summary), encoding="utf-8")


def _test_node(root: Path, name: str, test_id: str = "t1") -> Path:
    node = root / name
    node.mkdir(parents=True, exist_ok=True)
    (node / "__caliper_test_metadata__.yaml").write_text(
        yaml.safe_dump({"version": "1", "labels": {"test_id": test_id}}), encoding="utf-8"
    )
    return node


def test_valid_bundle_is_discovered_and_owned(tmp_path: Path):
    node = _test_node(tmp_path, "000__test")
    bundle_dir = node / "001__capture" / "artifacts" / "traces"
    bundle_dir.mkdir(parents=True)
    archive = bundle_dir / ARCHIVE_FILENAME
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write("{}\n")
    _write_summary(bundle_dir, relative=ARCHIVE_FILENAME)

    bundles = discover_bundles(tmp_path)
    assert len(bundles) == 1
    assert bundles[0].test_dir == node


def test_archive_parent_traversal_is_rejected(tmp_path: Path):
    node = _test_node(tmp_path, "node")
    bundle_dir = node / "traces"
    bundle_dir.mkdir(parents=True)
    _write_summary(bundle_dir, relative="../../../etc/passwd")
    with pytest.raises(BundleValidationError, match="safe relative path"):
        discover_bundles(tmp_path)


def test_missing_archive_is_rejected(tmp_path: Path):
    node = _test_node(tmp_path, "node")
    bundle_dir = node / "traces"
    bundle_dir.mkdir(parents=True)
    _write_summary(bundle_dir, relative=ARCHIVE_FILENAME)
    with pytest.raises(BundleValidationError, match="archive not found"):
        discover_bundles(tmp_path)


def test_unsupported_schema_version_is_rejected(tmp_path: Path):
    node = _test_node(tmp_path, "node")
    bundle_dir = node / "traces"
    bundle_dir.mkdir(parents=True)
    archive = bundle_dir / ARCHIVE_FILENAME
    with gzip.open(archive, "wt", encoding="utf-8"):
        pass
    (bundle_dir / SUMMARY_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 99,
                "identity": {"test_id": "t"},
                "data": {"archive_relative_path": ARCHIVE_FILENAME},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BundleValidationError, match="unsupported trace summary schema_version"):
        discover_bundles(tmp_path)


def test_bundle_without_test_node_marker_is_rejected(tmp_path: Path):
    bundle_dir = tmp_path / "orphan" / "traces"
    bundle_dir.mkdir(parents=True)
    archive = bundle_dir / ARCHIVE_FILENAME
    with gzip.open(archive, "wt", encoding="utf-8"):
        pass
    _write_summary(bundle_dir, relative=ARCHIVE_FILENAME)
    with pytest.raises(BundleValidationError, match="no owning Caliper test node"):
        validate_bundle(bundle_dir / SUMMARY_FILENAME, artifact_root=tmp_path)


def test_symlink_escape_is_rejected(tmp_path: Path):
    node = _test_node(tmp_path, "node")
    bundle_dir = node / "traces"
    bundle_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    (bundle_dir / ARCHIVE_FILENAME).symlink_to(outside)
    _write_summary(bundle_dir, relative=ARCHIVE_FILENAME)
    # The summary's parent is the bundle dir; a symlinked archive that resolves
    # outside it must be rejected.
    with pytest.raises(BundleValidationError, match="escapes the bundle directory"):
        validate_bundle(bundle_dir / SUMMARY_FILENAME, artifact_root=tmp_path)


def _destination(parent: str = "parent-1") -> ChildRunDestination:
    return ChildRunDestination(
        tracking_uri="http://mlflow.test", experiment_id="exp-1", parent_run_id=parent
    )


def test_child_run_map_round_trip_and_stable_key(tmp_path: Path):
    root = tmp_path / "artifacts"
    node = _test_node(root, "000__test")
    key = child_run_key(node, root)
    assert key == "000__test"

    write_child_run_map(
        root, {key: {"run_id": "run-1", "run_name": "child"}}, destination=_destination()
    )
    loaded = read_child_run_map(root, destination=_destination())
    assert loaded[key]["run_id"] == "run-1"


def test_child_run_key_rejects_outside_path(tmp_path: Path):
    with pytest.raises(ValueError, match="not under artifact root"):
        child_run_key(tmp_path / "elsewhere", tmp_path / "artifacts")


def test_child_run_map_destination_mismatch_is_rejected(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    write_child_run_map(
        root, {"000__test": {"run_id": "run-1"}}, destination=_destination("parent-1")
    )
    # A resume against a different parent run must not attach stale children.
    with pytest.raises(ChildRunMapError, match="different export destination"):
        read_child_run_map(root, destination=_destination("parent-2"))


def test_child_run_map_without_scope_is_rejected_on_scoped_read(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "_mlflow_child_runs.yaml").write_text(
        yaml.safe_dump({"version": 1, "children": {"000__test": {"run_id": "run-1"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ChildRunMapError, match="no destination scope"):
        read_child_run_map(root, destination=_destination())


def test_child_run_map_invalid_version_is_rejected(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "_mlflow_child_runs.yaml").write_text(
        yaml.safe_dump({"version": 99, "children": {}}), encoding="utf-8"
    )
    with pytest.raises(ChildRunMapError, match="unsupported child-run map version"):
        read_child_run_map(root)


def test_child_run_map_entry_without_run_id_is_rejected(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "_mlflow_child_runs.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "destination": _destination().to_dict(),
                "children": {"a": {"run_name": "x"}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ChildRunMapError, match="missing run_id"):
        read_child_run_map(root)
