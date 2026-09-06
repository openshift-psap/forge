"""Discover test base directories via caliper metadata file or MatrixBenchmarking settings.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.constants import (
    LEGACY_METADATA_FILE,
    MATRIXBENCHMARKING_SETTINGS_FILE,
    METADATA_FILE,
)
from projects.caliper.engine.model import TestBaseNode

# Primary marker
MARKER = METADATA_FILE
# Legacy marker for backwards compatibility
LEGACY_MARKER = LEGACY_METADATA_FILE
MATRIXBENCHMARKING_MARKER = MATRIXBENCHMARKING_SETTINGS_FILE


def discover_test_bases(
    base_dir: Path,
    *,
    include_label_filter: dict[str, list[str]] | None = None,
    exclude_label_filter: dict[str, list[str]] | None = None,
) -> tuple[list[TestBaseNode], list[dict[str, Any]]]:
    """Walk base_dir; each directory containing MARKER or MATRIXBENCHMARKING_MARKER becomes a TestBaseNode.

    Args:
        base_dir: Base directory to search
        include_label_filter: List of filter dicts or single dict of label key-value pairs that must match for inclusion
        exclude_label_filter: List of filter dicts or single dict of label key-value pairs that exclude directories if they match

    Returns:
        Tuple of (included nodes, excluded directories with reasons)
    """
    base_dir = base_dir.resolve()
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Base directory does not exist: {base_dir}")

    nodes: list[TestBaseNode] = []
    excluded_dirs: list[dict[str, Any]] = []
    for dirpath, _dirnames, filenames in os.walk(base_dir, topdown=True):
        marker_found = None
        if MARKER in filenames:
            marker_found = MARKER
        elif LEGACY_MARKER in filenames:
            marker_found = LEGACY_MARKER  # Backwards compatibility
        elif MATRIXBENCHMARKING_MARKER in filenames:
            marker_found = MATRIXBENCHMARKING_MARKER

        if marker_found is None:
            continue

        path = Path(dirpath)

        # Use hierarchical label loading for both marker types
        if marker_found == MARKER or marker_found == LEGACY_MARKER:
            test_labels = _load_hierarchical_test_labels(path, base_dir)
            # For filtering, use the labels directly (hierarchical loading returns the labels dict)
            # Normalize missing "labels" entry to empty mapping to allow discovery of empty marker files
            labels = test_labels.get("labels", {})
        else:
            # Use hierarchical loading for MatrixBenchmarking settings.yaml too
            labels = _load_hierarchical_labels_matrixbenchmarking(path, base_dir)
            test_labels = dict(
                path=str(path.relative_to(base_dir)),
                labels=labels,
                version="matrix_benchmarking/settings",
            )
        # Check for exclusion reasons
        relative_path = str(path.relative_to(base_dir))

        # Skip directory if skip: true is set at the top level of the labels file
        # or inside the labels section (both conventions are supported).
        if test_labels.get("skip") is True or labels.get("skip") is True:
            excluded_dirs.append(
                {
                    "path": relative_path,
                    "reason": "skip",
                    "detail": "skip: true set in labels",
                    "labels": labels.copy(),
                    "marker_type": marker_found,
                }
            )
            continue

        # Apply label filtering if specified

        from projects.caliper.engine.label_filters import matches_filters

        filter_labels = labels.copy()
        filter_labels.update(test_labels.get("kpi_labels", {}))

        def _matches_any_local(labels_dict: dict, key: str, filter_values: list[str]) -> bool:
            """Local copy of _matches_any for detailed filter reason reporting."""
            raw = labels_dict.get(key)
            for filter_value in filter_values:
                if filter_value == "not-set":
                    if key not in labels_dict:
                        return True
                elif str(raw) == filter_value:
                    return True
            return False

        filter_result = matches_filters(
            filter_labels,
            include=include_label_filter or {},
            exclude=exclude_label_filter or {},
        )

        if not filter_result:
            # Determine specific filter reason
            filter_reason = "filter_mismatch"
            filter_detail = []

            # Check exclude filters
            if exclude_label_filter:
                for key, values in exclude_label_filter.items():
                    if _matches_any_local(filter_labels, key, values):
                        filter_detail.append(f"excluded by {key}={values}")

            # Check include filters
            if include_label_filter and not filter_detail:
                for key, values in include_label_filter.items():
                    if not _matches_any_local(filter_labels, key, values):
                        filter_detail.append(f"does not match required {key}={values}")

            detail = "; ".join(filter_detail) if filter_detail else "label filter mismatch"
            excluded_dirs.append(
                {
                    "path": relative_path,
                    "reason": filter_reason,
                    "detail": detail,
                    "labels": filter_labels.copy(),
                    "marker_type": marker_found,
                }
            )
            continue

        nodes.append(
            TestBaseNode(
                directory=path,
                test_labels=test_labels,
                artifact_paths=_list_files_under(path, exclude_markers=True),
                test_path=path.relative_to(base_dir),
            )
        )
    return sorted(nodes, key=lambda n: str(n.directory)), excluded_dirs


def _load_hierarchical_test_labels(test_dir: Path, base_dir: Path) -> dict[str, Any]:
    """Load and merge test metadata files hierarchically from base_dir down to test_dir.

    Merges in order (for each directory):
    1. __caliper_test_metadata__.*.yaml (all variants, new format)
    2. __test_labels__.*.yaml (all variants, legacy format)
    3. __caliper_test_metadata__.yaml (final, new format)
    4. __test_labels__.yaml (final, legacy fallback)

    Later files override earlier ones, with the main metadata file having final priority.
    New format files are preferred over legacy format when both exist.
    """
    import glob

    merged_labels: dict[str, Any] = {}

    # Get all directories from base_dir down to test_dir (inclusive)
    test_dir_abs = test_dir.resolve()
    base_dir_abs = base_dir.resolve()

    # Build path from base_dir to test_dir
    try:
        rel_path = test_dir_abs.relative_to(base_dir_abs)
        path_parts = [base_dir_abs] + [
            base_dir_abs / Path(*rel_path.parts[: i + 1]) for i in range(len(rel_path.parts))
        ]
    except ValueError:
        # test_dir is not under base_dir, just use test_dir
        path_parts = [test_dir_abs]

    # For each directory in the hierarchy, merge variant files (new format first, then legacy)
    for dir_path in path_parts:
        if not dir_path.is_dir():
            continue

        # Find all variant files - new format first
        new_pattern = str(dir_path / f"{METADATA_FILE.replace('.yaml', '.*.yaml')}")
        legacy_pattern = str(dir_path / f"{LEGACY_METADATA_FILE.replace('.yaml', '.*.yaml')}")

        # Collect all variant files, prioritizing new format
        variant_files = []
        variant_files.extend(sorted(glob.glob(new_pattern)))
        variant_files.extend(sorted(glob.glob(legacy_pattern)))

        for variant_file in variant_files:
            variant_path = Path(variant_file)
            if variant_path.is_file():
                try:
                    variant_labels = _load_labels(variant_path, is_matrixbenchmarking=False)
                    # Merge the labels (later values override earlier ones)
                    _deep_merge_dict(merged_labels, variant_labels)
                except (OSError, yaml.YAMLError, ValueError):
                    # Skip files that can't be loaded
                    pass

    # Finally, load the main metadata file from the test directory (final priority)
    # Prefer new format, fall back to legacy format
    main_labels_path = test_dir / MARKER
    if not main_labels_path.is_file():
        main_labels_path = test_dir / LEGACY_MARKER

    if main_labels_path.is_file():
        try:
            main_labels = _load_labels(main_labels_path, is_matrixbenchmarking=False)
            _deep_merge_dict(merged_labels, main_labels)
        except (OSError, yaml.YAMLError, ValueError):
            # If main file can't be loaded, return what we have
            pass

    return merged_labels


def _load_hierarchical_labels_matrixbenchmarking(test_dir: Path, base_dir: Path) -> dict[str, Any]:
    """Load and merge settings.yaml files hierarchically from base_dir down to test_dir for MatrixBenchmarking.

    Merges in order:
    1. base_dir/settings.*.yaml (all variants)
    2. parent_dir/settings.*.yaml (all variants)
    3. test_dir/settings.*.yaml (all variants)
    4. test_dir/settings.yaml (final, cannot be overridden)

    Later files override earlier ones, with the main settings.yaml having final priority.
    """
    import glob

    merged_labels: dict[str, Any] = {}

    # Get all directories from base_dir down to test_dir (inclusive)
    test_dir_abs = test_dir.resolve()
    base_dir_abs = base_dir.resolve()

    # Build path from base_dir to test_dir
    try:
        rel_path = test_dir_abs.relative_to(base_dir_abs)
        path_parts = [base_dir_abs] + [
            base_dir_abs / Path(*rel_path.parts[: i + 1]) for i in range(len(rel_path.parts))
        ]
    except ValueError:
        # test_dir is not under base_dir, just use test_dir
        path_parts = [test_dir_abs]

    # For each directory in the hierarchy, merge settings.*.yaml files (excluding plain settings.yaml)
    for dir_path in path_parts:
        if not dir_path.is_dir():
            continue

        # Find all settings.*.yaml files (but not settings.yaml itself)
        pattern = str(dir_path / "settings.*.yaml")
        variant_files = sorted(glob.glob(pattern))

        for variant_file in variant_files:
            variant_path = Path(variant_file)
            if variant_path.is_file():
                try:
                    variant_labels = _load_labels(variant_path, is_matrixbenchmarking=True)
                    # Merge the labels (later values override earlier ones)
                    _deep_merge_dict(merged_labels, variant_labels)
                except (OSError, yaml.YAMLError, ValueError):
                    # Skip files that can't be loaded
                    pass

    # Finally, load the main settings.yaml from the test directory (final priority)
    main_labels_path = test_dir / MATRIXBENCHMARKING_MARKER
    if main_labels_path.is_file():
        try:
            main_labels = _load_labels(main_labels_path, is_matrixbenchmarking=True)
            _deep_merge_dict(merged_labels, main_labels)
        except (OSError, yaml.YAMLError, ValueError):
            # If main file can't be loaded, return what we have
            pass

    return merged_labels


def _deep_merge_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Deep merge source dict into target dict, with source values taking precedence."""
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge_dict(target[key], value)
        else:
            target[key] = value


def _load_labels(path: Path, is_matrixbenchmarking: bool = False) -> dict[str, Any]:
    """Load labels from either caliper metadata file or MatrixBenchmarking settings.yaml."""
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
