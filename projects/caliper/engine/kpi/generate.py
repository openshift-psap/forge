"""Emit canonical KPI JSON."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi.format import write_kpis_in_format
from projects.caliper.engine.parse import run_parse
from projects.caliper.engine.validation import load_schema, schema_path, validate_instance

logger = logging.getLogger(__name__)


def run_kpi_generate(
    *,
    base_dir: Path,
    plugin_module: str,
    plugin: object,
    output: Path | None,
    use_cache: bool,
    cache_path: Path | None,
    format_type: str = "hierarchical",
    include_label_filter: dict[str, list[str]] | None = None,
    exclude_label_filter: dict[str, list[str]] | None = None,
    verbose_parsing: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate KPI output in specified format.

    Args:
        base_dir: Directory containing test artifacts
        plugin_module: Name of the plugin module
        plugin: Plugin instance
        output: Path to write output file
        use_cache: Whether to use cached parse results
        cache_path: Path to cache file (not used currently)
        format_type: Output format - "hierarchical" (default) or "jsonl"
        include_label_filter: Include only directories matching these label filters
        exclude_label_filter: Exclude directories matching these label filters
        verbose_parsing: Enable verbose parsing output

    Returns:
        Tuple of (KPI records, status details). Status details includes default values
        when plugin.compute_kpis() returns only rows.
    """
    model = run_parse(
        base_dir=base_dir,
        plugin_module=plugin_module,
        plugin=plugin,
        use_cache=use_cache,
        include_label_filter=include_label_filter,
        exclude_label_filter=exclude_label_filter,
        verbose_parsing=verbose_parsing,
        show_parameter_matrix=verbose_parsing,  # Only show matrix in verbose mode
    )

    # Get KPIs from plugin - may return rows or (rows, status)
    result = plugin.compute_kpis(model)

    # Check if plugin returned status details
    if isinstance(result, tuple) and len(result) == 2:
        rows, status_obj = result

        # Handle both new dataclass and legacy dict formats
        from projects.caliper.engine.kpi import KpiComputationStatus

        if isinstance(status_obj, KpiComputationStatus):
            status = status_obj.status
            message = status_obj.message
            warnings = status_obj.warnings
            status_details = status_obj.to_dict()
        else:
            # Legacy dict format
            status = status_obj.get("status", "success")
            message = status_obj.get("message")
            warnings = status_obj.get("warnings", [])
            status_details = status_obj

        # Log warnings if present
        if warnings:
            for warning in warnings:
                logger.warning(f"KPI computation warning: {warning}")

        # Handle different status codes
        if status == "failed":
            raise RuntimeError(f"KPI computation failed: {message}")
        elif status == "warning":
            logger.warning(f"KPI computation completed with warnings: {message}")
    else:
        # Plugin returned just rows (old behavior) - provide default status details
        rows = result
        status_details = {"status": "success", "success": True, "message": None, "warnings": []}

    # Convert KpiRecord objects to dictionaries for validation and output
    from projects.caliper.engine.kpi import KpiRecord

    rows_as_dicts = []
    for row in rows:
        if isinstance(row, KpiRecord):
            rows_as_dicts.append(row.to_dict())
        else:
            rows_as_dicts.append(row)

    # Validate using dictionary representations
    kpi_schema = load_schema(schema_path("kpi_record.schema.json"))
    for row_dict in rows_as_dicts:
        validate_instance(row_dict, kpi_schema, "KPI record")

    if output:
        write_kpis_in_format(rows_as_dicts, output, format_type, model)

    # Always return tuple with KPI records and status details
    return rows, status_details
