"""KPI decorator classes for declarative KPI definitions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from projects.caliper.engine.kpi.dataclasses import KpiCatalogEntry


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


class Curve:
    """Decorator to mark KPIs as curve data (returning list of (x, y) tuples)."""

    def __init__(
        self,
        x_unit: str,
        y_unit: str | None = None,
        x_help: str | None = None,
        y_help: str | None = None,
        x_format: str | None = None,
        y_format: str | None = None,
    ):
        self.x_unit = x_unit
        self.y_unit = y_unit
        self.x_format = x_format
        self.y_format = y_format
        self.x_help = x_help
        self.y_help = y_help

    def __call__(self, func: Callable) -> Callable:
        func._kpi_is_curve = True
        func._kpi_x_unit = self.x_unit
        func._kpi_y_help = self.y_help
        func._kpi_x_help = self.x_help
        func._kpi_y_unit = self.y_unit
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


def is_curve_kpi(func: Callable) -> bool:
    """Check if a KPI function is marked as curve data."""
    return getattr(func, "_kpi_is_curve", False)


def build_catalog_from_functions(module) -> list[KpiCatalogEntry]:
    """
    Build KPI catalog from decorated functions in a module.

    Args:
        module: The module containing KPI functions

    Returns:
        List of KpiCatalogEntry dataclasses
    """
    catalog = []
    kpi_functions = get_kpi_functions(module)

    for kpi_id, func in kpi_functions.items():
        # Extract the display name from the docstring or function name
        name = (
            func.__doc__.replace(" KPI.", "") if func.__doc__ else kpi_id.replace("_", " ").title()
        )

        # Create KpiCatalogEntry dataclass
        if is_curve_kpi(func):
            catalog_entry = KpiCatalogEntry(
                kpi_id=kpi_id,
                name=name,
                higher_is_better=func._kpi_higher_is_better,
                is_curve=True,
                help=func._kpi_help,
                x_unit=func._kpi_x_unit,
                x_help=func._kpi_x_help,
                y_unit=func._kpi_y_unit,
                y_help=func._kpi_y_help,
            )
        else:
            catalog_entry = KpiCatalogEntry(
                kpi_id=kpi_id,
                name=name,
                unit=func._kpi_unit,
                higher_is_better=func._kpi_higher_is_better,
                is_curve=False,
                help=func._kpi_help,
            )

        catalog.append(catalog_entry)

    return catalog
