"""KPI output format transformations and utilities."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def transform_kpis_to_hierarchical_format(kpis: list[dict], model) -> dict:
    """
    Transform flat KPI list into hierarchical JSON structure.

    Groups KPIs by test (run_id), extracting common labels and organizing
    KPI metadata (name, help, unit, etc.) for improved readability.

    Args:
        kpis: List of flat KPI records from compute_kpis
        model: Unified model for accessing plugin metadata

    Returns:
        Hierarchical JSON structure organized by test
    """

    if not kpis:
        return {"schema_version": "2", "tests": []}

    # Group KPIs by test (run_id)
    tests_data = defaultdict(lambda: {"kpis": [], "labels": {}, "metadata": {}})

    # Get KPI function metadata from the plugin module
    from projects.caliper.engine.kpi.decorators import get_kpi_functions

    try:
        plugin_module_obj = __import__(model.plugin_module, fromlist=[""])
        kpi_functions = get_kpi_functions(plugin_module_obj)
    except (ImportError, AttributeError):
        kpi_functions = {}

    for kpi in kpis:
        run_id = kpi.get("run_id", "unknown")
        test_data = tests_data[run_id]

        # Extract common labels (excluding KPI-specific ones)
        kpi_labels = kpi.get("labels", {})
        test_labels = {
            k: v for k, v in kpi_labels.items() if k not in ["higher_is_better"]
        }  # Exclude KPI-specific labels

        # Validate and merge test labels from all KPIs in the test
        for key, value in test_labels.items():
            if key in test_data["labels"] and test_data["labels"][key] != value:
                raise ValueError(
                    f"Label mismatch for run_id '{run_id}': key '{key}' has conflicting values "
                    f"'{test_data['labels'][key]}' vs '{value}'"
                )
        test_data["labels"].update(test_labels)

        # Store test metadata from first KPI
        if not test_data["metadata"]:
            test_data["metadata"] = {
                "timestamp": kpi.get("timestamp"),
                "source": kpi.get("source", {}),
                "run_id": run_id,
            }

        # Create KPI record with metadata
        kpi_id = kpi.get("kpi_id")
        raw_value = kpi.get("value")

        # Resolve _kpi_is_2d first as source of truth when metadata exists
        is_2d = False
        if kpi_id in kpi_functions:
            func = kpi_functions[kpi_id]
            is_2d = getattr(func, "_kpi_is_2d", False)
        else:
            # Consistent fallback heuristic when metadata is absent
            is_2d = bool(
                isinstance(raw_value, list)
                and raw_value
                and isinstance(raw_value[0], (list, tuple))
                and len(raw_value[0]) == 2
            )

        # Apply tuple-pair structural transform only for confirmed 2D KPIs
        if is_2d and isinstance(raw_value, list) and raw_value and len(raw_value) > 0:
            first_item = raw_value[0]
            if isinstance(first_item, (list, tuple)) and len(first_item) == 2:
                try:
                    # Convert list of tuples [(x1, y1), (x2, y2), ...] to structured format
                    structured_value = {
                        "data_points": [{"x": float(x), "y": float(y)} for x, y in raw_value],
                        "count": len(raw_value),
                    }
                    final_value = structured_value
                except (ValueError, TypeError, IndexError):
                    # If conversion fails, preserve original list-of-tuples representation
                    final_value = raw_value
            else:
                # Preserve original list-of-tuples representation
                final_value = raw_value
        else:
            # Not 2D or no valid tuple-pair structure, keep original value
            final_value = raw_value

        kpi_record = {
            "id": kpi_id,
            "value": final_value,
            "unit": kpi.get("unit"),
            "higher_is_better": kpi_labels.get("higher_is_better", True),
            "is_2d": is_2d,  # Set is_2d consistently based on resolved value
        }

        # Add KPI metadata from function decorator if available
        if kpi_id in kpi_functions:
            func = kpi_functions[kpi_id]
            kpi_record.update(
                {
                    "name": (
                        func.__doc__.replace(" KPI.", "")
                        if func.__doc__
                        else kpi_id.replace("_", " ").title()
                    ),
                    "help": getattr(func, "_kpi_help", ""),
                }
            )

            # Add 2D-specific metadata if this is a 2D KPI
            if is_2d:
                kpi_record.update(
                    {
                        "x_unit": getattr(func, "_kpi_x_unit", ""),
                        "x_help": getattr(func, "_kpi_x_help", ""),
                        "y_unit": getattr(func, "_kpi_y_unit", None) or kpi_record["unit"],
                        "y_help": getattr(func, "_kpi_y_help", None)
                        or getattr(func, "_kpi_help", ""),
                    }
                )

            # Add formatting info if available
            if hasattr(func, "_kpi_format"):
                kpi_record["format"] = func._kpi_format
        else:
            # Fallback if no function metadata available
            kpi_record.update(
                {
                    "name": kpi_id.replace("_", " ").title(),
                    "help": f"KPI: {kpi_id}",
                }
            )

        test_data["kpis"].append(kpi_record)

    # Convert to final structure
    tests_list = []
    for run_id, test_data in tests_data.items():
        tests_list.append(
            {
                "run_id": run_id,
                "labels": test_data["labels"],
                "metadata": test_data["metadata"],
                "kpis": test_data["kpis"],
            }
        )

    return {"schema_version": "2", "tests": tests_list}


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
        hierarchical_data = transform_kpis_to_hierarchical_format(kpis, model)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(hierarchical_data, f, indent=2, ensure_ascii=False)
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
            # Hierarchical format - flatten it
            # Accumulate records in temporary list to ensure atomicity
            temp_kpis = []

            for test in data.get("tests", []):
                test_metadata = test.get("metadata", {})
                test_labels = test.get("labels", {})

                for kpi_record in test.get("kpis", []):
                    # Convert structured value to schema-v1 format if needed
                    raw_value = kpi_record.get("value")
                    converted_value = _convert_to_schema_v1_value(raw_value)

                    flat_kpi = {
                        "schema_version": "1",
                        "kpi_id": kpi_record.get("id"),
                        "value": converted_value,
                        "unit": kpi_record.get("unit"),
                        "run_id": test_metadata.get("run_id"),
                        "timestamp": test_metadata.get("timestamp"),
                        "labels": {
                            **test_labels,
                            "higher_is_better": kpi_record.get("higher_is_better", True),
                        },
                        "source": test_metadata.get("source", {}),
                    }

                    # Preserve schema-v2 metadata fields if present
                    metadata_fields = [
                        "is_2d",
                        "name",
                        "help",
                        "x_unit",
                        "y_unit",
                        "x_help",
                        "y_help",
                        "format",
                    ]
                    for field in metadata_fields:
                        if field in kpi_record:
                            flat_kpi[field] = kpi_record[field]
                    temp_kpis.append(flat_kpi)

            # Only merge into main kpis list after entire branch succeeds
            kpis.extend(temp_kpis)
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
