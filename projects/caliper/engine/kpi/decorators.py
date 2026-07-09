"""KPI decorator classes for declarative KPI definitions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class KPIMetadata:
    """Decorator to add metadata to KPI functions."""

    def __init__(self, help: str, unit: str):
        self.help = help
        self.unit = unit

    def __call__(self, func: Callable) -> Callable:
        func._kpi_help = self.help
        func._kpi_unit = self.unit
        return func


class HigherBetter:
    """Decorator to mark KPIs where higher values are better."""

    def __call__(self, func: Callable) -> Callable:
        func._kpi_higher_is_better = True
        return func


class LowerBetter:
    """Decorator to mark KPIs where lower values are better."""

    def __call__(self, func: Callable) -> Callable:
        func._kpi_higher_is_better = False
        return func


class Format:
    """Decorator to specify formatting for KPI values."""

    def __init__(self, format_str: str):
        self.format_str = format_str

    def __call__(self, func: Callable) -> Callable:
        func._kpi_format = self.format_str
        return func


class TwoDimensional:
    """Decorator to mark KPIs as 2D (returning list of (x, y) tuples)."""

    def __init__(
        self,
        x_unit: str,
        x_help: str,
        y_unit: str | None = None,
        y_help: str | None = None,
        x_format: str | None = None,
        y_format: str | None = None,
    ):
        self.x_unit = x_unit
        self.x_help = x_help
        self.y_unit = y_unit
        self.y_help = y_help
        self.x_format = x_format
        self.y_format = y_format

    def __call__(self, func: Callable) -> Callable:
        func._kpi_is_2d = True
        func._kpi_x_unit = self.x_unit
        func._kpi_x_help = self.x_help
        func._kpi_y_unit = self.y_unit
        func._kpi_y_help = self.y_help
        func._kpi_x_format = self.x_format
        func._kpi_y_format = self.y_format
        return func


class TestLabelExtractor:
    """
    Extract custom labels from test data for all KPIs in a test.

    Supports multiple extraction methods:
    1. Dictionary mapping - extract values using dot notation paths
    2. Callable function - custom extraction logic
    """

    def __init__(self, labels: dict[str, str] | Callable[[Any], dict[str, Any]]):
        self.labels = labels

    def extract(self, record: Any) -> dict[str, Any]:
        """Extract labels from a test record."""
        try:
            if callable(self.labels):
                # Function-based extraction
                labels = self.labels(record)
                return labels if isinstance(labels, dict) else {}
            elif isinstance(self.labels, dict):
                # Dictionary-based extraction using dot notation
                labels = {}
                for label_key, path in self.labels.items():
                    value = _extract_value_by_path(record, path)
                    if value is not None:
                        # Convert to string for consistency
                        labels[label_key] = str(value)
                return labels
        except Exception:
            # If extraction fails, return empty dict rather than crashing
            return {}

        return {}


def _extract_value_by_path(obj: Any, path: str) -> Any:
    """
    Extract value from nested object using dot notation.

    Examples:
        _extract_value_by_path(record, "metrics.model_name")
        _extract_value_by_path(record, "run_identity.gpu_count")
        _extract_value_by_path(record, "distinguishing_labels.workload")
    """
    try:
        current = obj
        for part in path.split("."):
            if hasattr(current, part):
                current = getattr(current, part)
            elif hasattr(current, "get") and callable(current.get):
                current = current.get(part)
            else:
                return None
        return current
    except (AttributeError, KeyError, TypeError):
        return None


def create_label_extractor(
    labels: dict[str, str] | Callable[[Any], dict[str, Any]],
) -> TestLabelExtractor:
    """
    Create a label extractor for test records.

    Args:
        labels: Either a dict mapping label names to paths, or a callable

    Returns:
        TestLabelExtractor instance
    """
    return TestLabelExtractor(labels)


def get_kpi_functions(module) -> dict[str, Callable]:
    """
    Get all KPI functions defined in a module.

    Args:
        module: The module to search for KPI functions

    Returns:
        Dict mapping KPI function names to their callables
    """
    import inspect

    kpi_functions = {}

    for name, obj in inspect.getmembers(module, inspect.isfunction):
        if (
            hasattr(obj, "_kpi_help")
            and hasattr(obj, "_kpi_unit")
            and hasattr(obj, "_kpi_higher_is_better")
        ):
            kpi_functions[name] = obj

    return kpi_functions


def is_2d_kpi(func: Callable) -> bool:
    """Check if a KPI function is marked as 2D."""
    return getattr(func, "_kpi_is_2d", False)


def build_catalog_from_functions(module) -> list[dict[str, Any]]:
    """
    Build KPI catalog from decorated functions in a module.

    Args:
        module: The module containing KPI functions

    Returns:
        List of KPI definitions
    """
    catalog = []
    kpi_functions = get_kpi_functions(module)

    for kpi_id, func in kpi_functions.items():
        # Extract the display name from the docstring or function name
        name = (
            func.__doc__.replace(" KPI.", "") if func.__doc__ else kpi_id.replace("_", " ").title()
        )

        kpi_def = {
            "kpi_id": kpi_id,
            "name": name,
            "unit": func._kpi_unit,
            "higher_is_better": func._kpi_higher_is_better,
            "help": func._kpi_help,
        }

        # Add 2D-specific metadata if this is a 2D KPI
        if is_2d_kpi(func):
            kpi_def.update(
                {
                    "is_2d": True,
                    "x_unit": func._kpi_x_unit,
                    "x_help": func._kpi_x_help,
                    "y_unit": getattr(func, "_kpi_y_unit", None) or func._kpi_unit,
                    "y_help": getattr(func, "_kpi_y_help", None) or func._kpi_help,
                }
            )

            # Add formatting info if available
            if hasattr(func, "_kpi_x_format"):
                kpi_def["x_format"] = func._kpi_x_format
            if hasattr(func, "_kpi_y_format"):
                kpi_def["y_format"] = func._kpi_y_format
            elif hasattr(func, "_kpi_format"):
                kpi_def["y_format"] = func._kpi_format
        else:
            kpi_def["is_2d"] = False
            # Add scalar formatting info if available
            if hasattr(func, "_kpi_format"):
                kpi_def["format"] = func._kpi_format

        catalog.append(kpi_def)

    return catalog
