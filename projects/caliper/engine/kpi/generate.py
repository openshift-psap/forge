"""Emit canonical KPI JSON."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi.format import write_kpis_in_format
from projects.caliper.engine.parse import run_parse
from projects.caliper.engine.validation import load_schema, schema_path, validate_instance


def run_kpi_generate(
    *,
    base_dir: Path,
    plugin_module: str,
    plugin: object,
    output: Path | None,
    use_cache: bool,
    cache_path: Path | None,
    format_type: str = "hierarchical",
) -> list[dict[str, Any]]:
    """Generate KPI output in specified format.

    Args:
        base_dir: Directory containing test artifacts
        plugin_module: Name of the plugin module
        plugin: Plugin instance
        output: Path to write output file
        use_cache: Whether to use cached parse results
        cache_path: Path to cache file (not used currently)
        format_type: Output format - "hierarchical" (default) or "jsonl"

    Returns:
        List of KPI records
    """
    model = run_parse(
        base_dir=base_dir,
        plugin_module=plugin_module,
        plugin=plugin,
        use_cache=use_cache,
    )
    compute = plugin.compute_kpis
    rows: list[dict[str, Any]] = compute(model)
    kpi_schema = load_schema(schema_path("kpi_record.schema.json"))
    for row in rows:
        validate_instance(row, kpi_schema, "KPI record")

    if output:
        write_kpis_in_format(rows, output, format_type, model)

    return rows
