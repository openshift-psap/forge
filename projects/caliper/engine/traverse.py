"""Discover test base directories via __test_labels__.yaml or MatrixBenchmarking settings.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.model import TestBaseNode

MARKER = "__test_labels__.yaml"
MATRIXBENCHMARKING_MARKER = "settings.yaml"


def discover_test_bases(base_dir: Path) -> list[TestBaseNode]:
    """Walk base_dir; each directory containing MARKER or MATRIXBENCHMARKING_MARKER becomes a TestBaseNode."""
    base_dir = base_dir.resolve()
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Base directory does not exist: {base_dir}")

    nodes: list[TestBaseNode] = []
    for dirpath, _dirnames, filenames in os.walk(base_dir, topdown=True):
        marker_found = None
        if MARKER in filenames:
            marker_found = MARKER
        elif MATRIXBENCHMARKING_MARKER in filenames:
            marker_found = MATRIXBENCHMARKING_MARKER

        if marker_found is None:
            continue

        path = Path(dirpath)
        marker_path = path / marker_found
        labels = _load_labels(
            marker_path, is_matrixbenchmarking=(marker_found == MATRIXBENCHMARKING_MARKER)
        )
        nodes.append(
            TestBaseNode(
                directory=path,
                labels=labels,
                artifact_paths=_list_files_under(path, exclude_markers=True),
                test_path=path.relative_to(base_dir),
            )
        )
    return sorted(nodes, key=lambda n: str(n.directory))


def _load_labels(path: Path, is_matrixbenchmarking: bool = False) -> dict[str, Any]:
    """Load labels from either __test_labels__.yaml or MatrixBenchmarking settings.yaml."""
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if data is None:
        return {}
    if not isinstance(data, dict):
        marker_name = MATRIXBENCHMARKING_MARKER if is_matrixbenchmarking else MARKER
        raise ValueError(f"Invalid {marker_name}: top level must be a mapping: {path}")

    # For MatrixBenchmarking settings.yaml, add metadata to distinguish the source
    if is_matrixbenchmarking:
        # Add a special label to indicate this came from MatrixBenchmarking
        result = dict(data)
        result["__caliper_source__"] = "matrixbenchmarking"
        return result

    return data


def _list_files_under(dirpath: Path, *, exclude_markers: bool) -> list[Path]:
    """List all files under dirpath, optionally excluding both marker files."""
    out: list[Path] = []
    excluded_names = {MARKER, MATRIXBENCHMARKING_MARKER} if exclude_markers else set()
    for p in sorted(dirpath.rglob("*")):
        if p.is_file() and (not exclude_markers or p.name not in excluded_names):
            out.append(p)
    return out
