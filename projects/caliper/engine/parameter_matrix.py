"""Parameter matrix analysis for Caliper results."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from projects.caliper.engine.model import UnifiedResultRecord


def analyze_parameter_matrix(records: list[UnifiedResultRecord]) -> dict[str, Any]:
    """
    Analyze the parameter space across all unified result records.

    Args:
        records: List of unified result records to analyze

    Returns:
        Dictionary containing parameter matrix analysis
    """
    if not records:
        return {
            "total_records": 0,
            "parameter_keys": [],
            "parameter_values": {},
            "parameter_combinations": [],
            "coverage_matrix": {},
            "summary": "No records to analyze",
        }

    # Collect all parameter keys and values
    parameter_keys = set()
    parameter_values = defaultdict(set)
    parameter_combinations = []

    for record in records:
        labels = record.distinguishing_labels
        parameter_keys.update(labels.keys())

        # Track unique parameter combinations
        param_combo = dict(labels.items())
        if param_combo not in parameter_combinations:
            parameter_combinations.append(param_combo)

        # Track all possible values for each parameter
        for key, value in labels.items():
            parameter_values[key].add(str(value))

    # Sort parameter keys for consistent output
    sorted_parameter_keys = sorted(parameter_keys)

    # Convert sets to sorted lists for JSON serialization
    parameter_value_lists = {key: sorted(values) for key, values in parameter_values.items()}

    # Create coverage matrix showing which parameter combinations exist
    coverage_matrix = {}
    for key in sorted_parameter_keys:
        coverage_matrix[key] = {}
        for value in parameter_value_lists[key]:
            coverage_matrix[key][value] = sum(
                1 for record in records if str(record.distinguishing_labels.get(key)) == value
            )

    return {
        "total_records": len(records),
        "total_parameter_combinations": len(parameter_combinations),
        "parameter_keys": sorted_parameter_keys,
        "parameter_values": parameter_value_lists,
        "parameter_combinations": parameter_combinations,
        "coverage_matrix": coverage_matrix,
        "summary": f"Found {len(records)} records across {len(parameter_combinations)} parameter combinations",
    }


def format_parameter_matrix_summary(analysis: dict[str, Any], max_combinations: int = 20) -> str:
    """
    Format parameter matrix analysis into a readable summary.

    Args:
        analysis: Result from analyze_parameter_matrix()
        max_combinations: Maximum number of combinations to display

    Returns:
        Formatted string summary
    """
    if analysis["total_records"] == 0:
        return "📊 Parameter Matrix: No records found"

    lines = []
    lines.append("📊 Parameter Matrix Summary")
    lines.append("=" * 50)
    lines.append(f"Total Records: {analysis['total_records']}")
    lines.append(f"Parameter Combinations: {analysis['total_parameter_combinations']}")
    lines.append("")

    # Show parameter keys and their possible values
    lines.append("🏷️  Parameter Keys:")
    for key in analysis["parameter_keys"]:
        values = analysis["parameter_values"][key]
        if len(values) <= 10:
            values_str = ", ".join(values)
        else:
            values_str = f"{', '.join(values[:10])} ... (+{len(values) - 10} more)"
        lines.append(f"   {key}: {values_str}")

    lines.append("")

    # Split parameters into varying (>1 value) and constant (==1 value)
    total_records = analysis["total_records"]
    varying_keys = []
    constant_keys = []
    for key in analysis["parameter_keys"]:
        if len(analysis["parameter_values"][key]) > 1:
            varying_keys.append(key)
        else:
            constant_keys.append(key)

    def _format_coverage(keys):
        for key in keys:
            coverage = analysis["coverage_matrix"][key]
            total_for_param = sum(coverage.values())
            missing = total_records - total_for_param
            missing_str = f" ({missing} undefined)" if missing else ""
            lines.append(f"   {key}: {total_for_param} records{missing_str}")
            for value, count in coverage.items():
                lines.append(f"      {value}: {count} records")

    lines.append(f"📈 Varying Parameters ({len(varying_keys)}):")
    if varying_keys:
        _format_coverage(varying_keys)
    else:
        lines.append("   (none)")

    lines.append("")
    lines.append(f"📌 Constant Parameters ({len(constant_keys)}):")
    if constant_keys:
        _format_coverage(constant_keys)
    else:
        lines.append("   (none)")

    # Show some example parameter combinations
    lines.append("")
    combinations = analysis["parameter_combinations"]
    display_count = min(max_combinations, len(combinations))
    lines.append(f"🔗 Parameter Combinations (showing {display_count} of {len(combinations)}):")

    for i, combo in enumerate(combinations[:display_count]):
        combo_str = ", ".join(f"{k}={v}" for k, v in combo.items())
        lines.append(f"   {i + 1:2d}. {combo_str}")

    if len(combinations) > max_combinations:
        lines.append(f"   ... ({len(combinations) - max_combinations} more combinations)")

    return "\n".join(lines)


def get_unique_parameter_values(
    records: list[UnifiedResultRecord], parameter_key: str
) -> list[str]:
    """
    Get all unique values for a specific parameter key across all records.

    Args:
        records: List of unified result records
        parameter_key: The parameter key to analyze

    Returns:
        Sorted list of unique values for the parameter
    """
    values = set()
    for record in records:
        if parameter_key in record.distinguishing_labels:
            values.add(str(record.distinguishing_labels[parameter_key]))
    return sorted(values)


def filter_records_by_parameters(
    records: list[UnifiedResultRecord], parameter_filter: dict[str, Any]
) -> list[UnifiedResultRecord]:
    """
    Filter records by parameter values.

    Args:
        records: List of unified result records to filter
        parameter_filter: Dictionary of parameter key-value pairs to match

    Returns:
        Filtered list of records matching the parameter criteria
    """
    filtered = []
    for record in records:
        matches = True
        for key, value in parameter_filter.items():
            if record.distinguishing_labels.get(key) != value:
                matches = False
                break
        if matches:
            filtered.append(record)
    return filtered


def _key_varies(records: list[UnifiedResultRecord], key: str) -> bool:
    """Check whether a label key takes more than one value across records."""
    values = {
        str(record.distinguishing_labels[key])
        for record in records
        if key in record.distinguishing_labels
    }
    return len(values) > 1


def get_varying_parameters(
    records: list[UnifiedResultRecord],
    priority_keys: list[str] | None = None,
) -> list[str]:
    """
    Get parameter keys that have varying values across records, in legend order.

    Every varying key is returned: filtering the legend to a configured subset
    hides real differences between runs. When ``priority_keys`` is provided,
    the varying keys it names are placed first (in that order); the remaining
    varying keys follow, sorted alphabetically.

    Args:
        records: List of unified result records to analyze
        priority_keys: When provided, these keys are ordered first in the
            result. Keys that do not vary across records are never included.

    Returns:
        Ordered list of parameter keys that vary across records
    """
    if not records:
        return []

    all_keys: set[str] = set()
    for record in records:
        all_keys.update(record.distinguishing_labels.keys())

    varying = sorted(key for key in all_keys if _key_varies(records, key))

    if not priority_keys:
        return varying

    prioritized = [key for key in dict.fromkeys(priority_keys) if key in varying]
    remaining = [key for key in varying if key not in prioritized]
    return prioritized + remaining


def create_legend_name(
    record: UnifiedResultRecord, varying_params: list[str] | set[str], max_length: int = 50
) -> str:
    """
    Create a meaningful legend name using only varying parameters.

    When exactly one parameter varies, shows value only ("XS").
    When multiple parameters vary, shows key=value pairs ("endpoint=maas, payload=XS"),
    in the order given by ``varying_params`` — configured priority keys first,
    see ``get_varying_parameters``. Set inputs are sorted alphabetically.

    Args:
        record: The record to create a name for
        varying_params: Varying parameter keys, in display order
        max_length: Maximum length for the legend name

    Returns:
        Legend name string
    """
    if not varying_params:
        return "default"

    if isinstance(varying_params, set | frozenset):
        present_keys = sorted(k for k in varying_params if k in record.distinguishing_labels)
    else:
        present_keys = [k for k in varying_params if k in record.distinguishing_labels]

    if not present_keys:
        return "default"

    if len(varying_params) == 1:
        return str(record.distinguishing_labels[present_keys[0]])

    return ", ".join(f"{k}={record.distinguishing_labels[k]}" for k in present_keys)


def create_legend_title(varying_params: list[str] | set[str]) -> str | None:
    """Return a legend title from the varying parameter key names.

    With a single key the title tells the reader what the bare values mean.
    With multiple keys it is redundant (entries already show key=value) so
    returns None.
    """
    if not varying_params or len(varying_params) > 1:
        return None

    return next(iter(varying_params))
