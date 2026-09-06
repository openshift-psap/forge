"""KPI output format transformations and utilities."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi.dataclasses import (
    HierarchicalKpi,
    HierarchicalKpiFormat,
    HierarchicalTestEntry,
    KpiCatalogEntry,
    TestMetadata,
)

logger = logging.getLogger(__name__)


def transform_kpis_to_hierarchical_format(kpis: list[dict], model) -> HierarchicalKpiFormat:
    """
    Transform flat KPI list into hierarchical JSON structure using dataclasses.

    Groups KPIs by test (run_id) using HierarchicalTestEntry dataclasses,
    with TestMetadata for test metadata and direct field access from
    KpiCatalogEntry dataclasses for improved type safety.

    The plugin catalog is used for *enrichment* only: KPIs that appear in
    the catalog get richer metadata; KPIs not in the catalog are still
    included using fields from the flat KpiRecord (is_curve, unit, etc.).

    Args:
        kpis: List of flat KPI records from compute_kpis
        model: Unified model for accessing plugin metadata

    Returns:
        HierarchicalKpiFormat dataclass containing structured test entries
        with TestMetadata and KPI data
    """

    if not kpis:
        return HierarchicalKpiFormat()

    # Group KPIs by test (run_id) using dataclasses
    tests_data: dict[str, HierarchicalTestEntry] = {}

    # Build catalog index for enrichment (not validation)
    kpi_models: dict[str, dict] = {}
    try:
        plugin_module_obj = __import__(model.plugin_module, fromlist=[""])
        kpi_catalog = plugin_module_obj.get_plugin().kpi_catalog()
    except Exception:
        logger.warning("Could not load KPI catalog for enrichment", exc_info=True)
        kpi_catalog = None

    if kpi_catalog:
        # Normalize catalog entries to KpiCatalogEntry dataclasses for consistent indexing
        kpi_models = {}
        for entry in kpi_catalog:
            if isinstance(entry, dict):
                # Convert dictionary to KpiCatalogEntry dataclass
                catalog_entry = KpiCatalogEntry.from_dict(entry)
            else:
                # Already a KpiCatalogEntry instance
                catalog_entry = entry
            kpi_models[catalog_entry.kpi_id] = catalog_entry

    # First pass: determine which labels vary across KPIs in the same run
    run_label_values: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for kpi in kpis:
        run_id = kpi.get("run_id", "unknown")
        for k, v in kpi.get("labels", {}).items():
            run_label_values[run_id][k].add(str(v))

    per_kpi_label_keys: dict[str, set[str]] = {}
    for run_id, label_vals in run_label_values.items():
        per_kpi_label_keys[run_id] = {k for k, vals in label_vals.items() if len(vals) > 1}

    for kpi in kpis:
        kpi_id = kpi.get("kpi_id")
        run_id = kpi.get("run_id", "unknown")

        # Get or create test entry using dataclass
        if run_id not in tests_data:
            tests_data[run_id] = HierarchicalTestEntry(run_id=run_id)

        test_data = tests_data[run_id]
        varying_keys = per_kpi_label_keys.get(run_id, set())

        # Split labels: constant → test-level, varying → per-KPI
        kpi_labels = kpi.get("labels", {})
        test_labels = {k: v for k, v in kpi_labels.items() if k not in varying_keys}
        test_data.labels.update(test_labels)

        # Store test metadata from first KPI
        if not test_data.metadata.timestamp:
            test_data.metadata = TestMetadata(
                timestamp=kpi.get("timestamp", ""),
                run_id=run_id,
            )

        # Get catalog entry or create one from the KPI record
        catalog_entry = kpi_models.get(kpi_id)
        if catalog_entry is None:
            # Create catalog entry from flat KPI record
            catalog_entry = KpiCatalogEntry(
                kpi_id=kpi_id,
                name=kpi.get("name") or kpi_id,
                unit=kpi.get("unit", ""),
                higher_is_better=kpi.get("higher_is_better", True),
                is_curve=kpi.get("is_curve", False),
                help=kpi.get("help", ""),
                x_unit=kpi.get("x_unit", ""),
                x_help=kpi.get("x_help", ""),
                y_unit=kpi.get("y_unit", ""),
                y_help=kpi.get("y_help", ""),
            )

        # Get the appropriate value field based on curve type
        if catalog_entry.is_curve:
            raw_value = kpi.get("values")
            if raw_value is None:
                raw_value = kpi.get("value", [])  # Backward compatibility
        else:
            raw_value = kpi.get("value")

        # Build KPI record using catalog entry (either real or constructed from KPI)
        kpi_record = {
            "kpi_id": kpi_id,
            "name": catalog_entry.name,
            "unit": catalog_entry.unit,
            "higher_is_better": catalog_entry.higher_is_better,
            "is_curve": catalog_entry.is_curve,
            "help": catalog_entry.help,
            "x_unit": catalog_entry.x_unit,
            "x_help": catalog_entry.x_help,
            "y_unit": catalog_entry.y_unit,
            "y_help": catalog_entry.y_help,
        }

        if catalog_entry.is_curve:
            kpi_record["values"] = raw_value  # Raw coordinate pairs for curve KPIs
        else:
            kpi_record["value"] = raw_value  # Scalar value for scalar KPIs

        # Build output record using HierarchicalKpi dataclass
        kpi_output = HierarchicalKpi(**kpi_record)

        test_data.kpis.append(kpi_output)

    # Convert to hierarchical format using dataclass
    hierarchical_format = HierarchicalKpiFormat(
        tests=list(tests_data.values()),
    )

    return hierarchical_format


def write_kpis_in_format(
    kpis: list[dict], output_file: Path, format_type: str = "hierarchical", model: Any = None
) -> None:
    """
    Write KPIs to file in the specified format.

    Args:
        kpis: List of KPI records
        output_file: Path to output file
        format_type: Format type - "hierarchical" (default) or "jsonl"
        model: Unified model (required for hierarchical format)
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if format_type == "hierarchical":
        if model is None:
            raise ValueError("Model is required for hierarchical format")

        # Transform to hierarchical format (schema v2)
        hierarchical_format = transform_kpis_to_hierarchical_format(kpis, model)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(hierarchical_format.to_dict(), f, indent=2, ensure_ascii=False)
            # Add EOL at EOF if we have data
            if kpis:
                f.write("\n")

    elif format_type == "jsonl":
        # Write as JSONL (schema v1) - matches original behavior
        text = "\n".join(json.dumps(kpi, ensure_ascii=False) for kpi in kpis) + (
            "\n" if kpis else ""
        )
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(text)

    else:
        raise ValueError(f"Unknown format type: {format_type}. Use 'hierarchical' or 'jsonl'")


def flatten_hierarchical_kpis(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Lossless conversion from schema_version=2 hierarchical KPI doc to flat records.

    Each output record contains:
    - All test-level fields: run_id, labels, metadata
    - All kpi-level fields verbatim, with 'id' renamed to 'kpi_id'

    The value is kept as-is (no conversion). Callers that need schema-v1 value
    representation should apply _convert_to_schema_v1_value() separately.
    """
    records: list[dict[str, Any]] = []
    for test in data.get("tests", []):
        test_base = {
            "run_id": test.get("run_id"),
            "labels": test.get("labels", {}),
            "metadata": test.get("metadata", {}),
        }
        for kpi in test.get("kpis", []):
            record = dict(test_base)
            record["kpi_id"] = kpi.get("kpi_id")
            for k, v in kpi.items():
                if k == "kpi_id":
                    pass
                elif k == "labels":
                    record[k] = {} | test_base["labels"] | v
                else:
                    record[k] = v

            records.append(record)
    return records


def _convert_to_schema_v1_value(raw_value: Any) -> Any:
    """
    Convert structured KPI value back to schema-v1 list-of-pairs representation.

    Args:
        raw_value: The KPI value, either scalar or structured with data_points/count

    Returns:
        Converted value - list of pairs for 2D data, scalar values unchanged
    """
    if isinstance(raw_value, dict):
        # Check if this is a structured value with data_points
        if "data_points" in raw_value and isinstance(raw_value["data_points"], list):
            # Convert data_points list back to list-of-pairs format
            data_points = raw_value["data_points"]
            return [
                [point.get("x"), point.get("y")]
                for point in data_points
                if isinstance(point, dict) and "x" in point and "y" in point
            ]
        # For other dict structures, return as-is (preserve existing format)
        return raw_value
    else:
        # Preserve scalar values unchanged
        return raw_value


def read_kpis_from_file(file_path: Path) -> list[dict]:
    """
    Read KPIs from a file, handling both JSONL and hierarchical JSON formats.

    Args:
        file_path: Path to the KPI file

    Returns:
        List of KPI records in flat format
    """
    kpis = []

    with open(file_path, encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        return kpis

    try:
        # Try to parse as JSON (hierarchical format)
        data = json.loads(content)

        if isinstance(data, dict) and data.get("schema_version") == "2":
            for rec in flatten_hierarchical_kpis(data):
                rec["value"] = _convert_to_schema_v1_value(rec.get("value"))
                kpis.append(rec)
        else:
            # Unknown JSON format
            raise ValueError("Unknown JSON format")

    except json.JSONDecodeError:
        # Try to parse as JSONL
        for line in content.splitlines():
            line = line.strip()
            if line:
                try:
                    kpi = json.loads(line)
                    kpis.append(kpi)
                except json.JSONDecodeError:
                    continue  # Skip invalid lines

    return kpis
