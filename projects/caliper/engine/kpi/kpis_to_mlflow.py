"""Generic kpis.json -> metrics.json + parameters.json conversion.

Reads a hierarchical kpis.json (schema v2) and writes per-test-run
metrics.json and parameters.json files into the matching artifact tree
directories. The MLflow export backend picks these up automatically via
``_log_metrics_and_params_from_tree``.

This replaces project-specific metrics.json generation (e.g. in
mcp_gateway parsers) with a single generic caliper mechanism that works
for every project producing a kpis.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.constants import (
    LEGACY_METADATA_FILE,
    METADATA_FILE,
    METRICS_FILE,
    PARAMETERS_FILE,
)
from projects.caliper.engine.kpi.dataclasses import HierarchicalKpiFormat
from projects.caliper.engine.kpi.report_dataclasses import MlflowConversionResult

logger = logging.getLogger(__name__)


def _build_run_dir_index(artifact_tree: Path) -> dict[str, Path]:
    """Map run directory names to their paths using metadata markers (with backwards compatibility)."""
    index: dict[str, Path] = {}

    # Collect directories with either metadata file (new format or legacy)
    metadata_dirs = set()

    for marker in artifact_tree.rglob(METADATA_FILE):
        if marker.is_file():
            metadata_dirs.add(marker.parent)

    # Look for legacy format (for directories that don't have new format)
    for marker in artifact_tree.rglob(LEGACY_METADATA_FILE):
        if marker.is_file() and marker.parent not in metadata_dirs:
            metadata_dirs.add(marker.parent)

    # Build index from collected directories
    for run_dir in sorted(metadata_dirs):
        try:
            rel = run_dir.relative_to(artifact_tree)
        except ValueError:
            rel = Path(run_dir.name)
        index[str(rel)] = run_dir
        index[run_dir.name] = run_dir

    return index


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def generate_metrics_from_kpis(
    kpis_json_path: Path,
    artifact_tree: Path,
) -> MlflowConversionResult:
    """Convert kpis.json into per-run metrics.json and parameters.json files.

    For each test entry in kpis.json, finds the matching directory under
    ``artifact_tree`` (via caliper metadata file markers) and writes:

    - ``metrics.json``: ``{kpi_id: value}`` for all scalar KPIs
    - ``parameters.json``: test-level labels as string key-value pairs

    Args:
        kpis_json_path: Path to the kpis.json file (schema v2).
        artifact_tree: Root of the caliper artifact tree containing
            test run directories with caliper metadata file markers.

    Returns:
        Status dict with counts and any warnings.
    """
    if not kpis_json_path.is_file():
        raise FileNotFoundError(f"kpis.json not found: {kpis_json_path}")

    with kpis_json_path.open(encoding="utf-8") as f:
        raw_data = json.load(f)

    if not isinstance(raw_data, dict) or raw_data.get("schema_version") != "2":
        return MlflowConversionResult(status="skipped", reason="Not a schema v2 kpis.json")

    # Parse into typed dataclass structure
    try:
        kpi_data = HierarchicalKpiFormat.from_dict(raw_data)
    except Exception as e:
        logger.error("Failed to parse KPI data: %s", e)
        return MlflowConversionResult(status="skipped", reason=f"Invalid KPI data structure: {e}")

    if not kpi_data.tests:
        return MlflowConversionResult(status="skipped", reason="No tests in kpis.json")

    run_dir_index = _build_run_dir_index(artifact_tree)
    if not run_dir_index:
        error_msg = f"No test run directories found under {artifact_tree}"
        logger.error(error_msg)
        return MlflowConversionResult(
            status="failed",
            error=error_msg,
            tests_processed=0,
            total_tests=len(kpi_data.tests),
        )

    written = 0
    warnings: list[str] = []

    for test in kpi_data.tests:
        # Determine test base path for directory matching
        test_base_path = test.run_id

        run_dir = run_dir_index.get(test_base_path) or run_dir_index.get(test.run_id)
        if run_dir is None:
            warnings.append(f"No matching directory for run_id={test.run_id!r}")
            continue

        # Process KPIs using the simplified structure
        metrics: dict[str, Any] = {}
        for kpi in test.kpis:
            if kpi.is_curve:
                # Convert coordinate pairs to point dictionaries for MLflow
                if kpi.values:
                    # Validate that x values are integers (MLflow step values must be integers)
                    curve_points = []
                    for i, (x, y) in enumerate(kpi.values):
                        if not isinstance(x, (int, float)) or x != int(x):
                            raise ValueError(
                                f"Curve KPI '{kpi.kpi_id}' in test '{test.run_id}': "
                                f"data point {i} has non-integer step x={x!r} "
                                f"(MLflow steps must be integers)"
                            )
                        curve_points.append({"x": float(x), "y": float(y)})
                    metrics[kpi.kpi_id] = curve_points
            else:
                # For scalar KPIs, use the value field
                if kpi.value is not None:
                    metrics[kpi.kpi_id] = kpi.value

        # Write files with error handling
        try:
            if metrics:
                _write_json(run_dir / METRICS_FILE, metrics)

            # Process labels with type safety
            if test.labels:
                params = {str(k): ("" if v is None else str(v)) for k, v in test.labels.items()}
                _write_json(run_dir / PARAMETERS_FILE, params)

            written += 1
        except OSError as e:
            warnings.append(f"Failed to write files for run_id={test.run_id!r}: {e}")
            continue

    # Determine appropriate status based on results
    total_tests = len(kpi_data.tests)

    if written == 0:
        # No tests were processed - this is a failure, not success
        error_msg = f"Failed to process any of {total_tests} test(s). " + (
            f"Warnings: {'; '.join(warnings)}" if warnings else "No matching directories found."
        )
        result = MlflowConversionResult(
            status="failed",
            error=error_msg,
            tests_processed=0,
            total_tests=total_tests,
            warnings=warnings,
        )

        logger.error("Failed to process any tests from %s: %s", kpis_json_path.name, error_msg)
        return result

    elif written < total_tests:
        # Partial success - some tests processed but some failed
        result = MlflowConversionResult(
            status="success",  # Still success but with warnings
            tests_processed=written,
            total_tests=total_tests,
            partial=True,
            message=f"Processed {written}/{total_tests} tests successfully, {total_tests - written} failed",
            warnings=warnings,
        )
    else:
        # Full success - all tests processed
        result = MlflowConversionResult(
            status="success",
            tests_processed=written,
            total_tests=total_tests,
            warnings=warnings,
        )

    # Log warnings
    for w in warnings:
        logger.warning("kpis-to-metrics: %s", w)

    logger.info(
        "Generated metrics.json for %d/%d test(s) from %s",
        written,
        len(kpi_data.tests),
        kpis_json_path.name,
    )
    return result
