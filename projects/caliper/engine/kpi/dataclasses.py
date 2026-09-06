"""
Core dataclasses for Caliper KPI analysis.

Provides strongly-typed data structures for KPI records and hierarchical formats
used across all Caliper plugins. Report-related dataclasses are in report_dataclasses.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _convert_historical_kpi_data(data: dict[str, Any]) -> dict[str, Any]:
    """Convert historical KPI format to current format.

    Historical format uses:
    - 'id' instead of 'kpi_id'
    - 'is_2d' instead of 'is_curve'
    - value.data_points structure for curve data
    """

    # Convert historical format
    converted = data.copy()

    # Convert ID field
    converted["kpi_id"] = data["id"]

    # Convert curve flag
    converted["is_curve"] = data.get("is_2d", False) or data.get("is_curve", False)

    # Convert curve data structure
    if converted["is_curve"] and isinstance(data.get("value"), dict):
        data_points = data["value"].get("data_points", [])
        converted["values"] = [
            [point["x"], point["y"]] for point in data_points if isinstance(point, dict)
        ]
        converted["value"] = None  # Clear value since it's curve data

    return converted


@dataclass
class HierarchicalKpi:
    """Base KPI structure with common fields for both hierarchical and flat formats."""

    kpi_id: str  # KPI identifier
    value: Any = None  # For scalar KPIs
    values: list[list[float]] = field(
        default_factory=list
    )  # For curve KPIs (list of [x, y] coordinate pairs)
    name: str = ""
    unit: str = ""
    higher_is_better: bool = True
    is_curve: bool = False
    help: str = ""  # noqa: A003
    # Curve KPI support fields
    x_unit: str = ""
    x_help: str = ""
    y_unit: str = ""
    y_help: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)

        # Include correct value field based on curve type
        if self.is_curve:
            # For curve KPIs: include 'values', remove 'value' and scalar-specific fields
            result.pop("value", None)
            result.pop("unit", None)
        else:
            # For scalar KPIs: include 'value', remove 'values' and curve-specific fields
            result.pop("values", None)
            result.pop("x_unit", None)
            result.pop("y_unit", None)
            result.pop("x_help", None)
            result.pop("y_help", None)

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HierarchicalKpi:
        """Create HierarchicalKpi from dictionary data."""

        # Convert historical format to current format if needed
        if "id" in data or "kpi_id" not in data:
            data = _convert_historical_kpi_data(data)

        return cls(
            kpi_id=data.get("kpi_id", ""),
            value=data.get("value"),
            values=data.get("values", []),
            name=data.get("name", ""),
            unit=data.get("unit", ""),
            higher_is_better=data.get("higher_is_better", False),
            is_curve=data.get("is_curve", False),
            help=data.get("help", ""),
            x_unit=data.get("x_unit", ""),
            x_help=data.get("x_help", ""),
            y_unit=data.get("y_unit", ""),
            y_help=data.get("y_help", ""),
        )


@dataclass
class KpiRecord(HierarchicalKpi):
    """Core KPI record structure used by all plugins - extends HierarchicalKpi with flat format fields."""

    # Flat format specific fields
    schema_version: str = "1"
    labels: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)

        # Include correct value field based on curve type
        if self.is_curve:
            # For curve KPIs: include 'values', remove 'value' and scalar-specific fields
            result.pop("value", None)
            result.pop("unit", None)
        else:
            # For scalar KPIs: include 'value', remove 'values' and curve-specific fields
            result.pop("values", None)
            result.pop("x_unit", None)
            result.pop("x_help", None)
            result.pop("y_unit", None)
            result.pop("y_help", None)

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KpiRecord:
        """Create KpiRecord from dictionary data."""
        is_curve = data.get("is_curve", False)

        return cls(
            kpi_id=data["kpi_id"],
            value=data.get("value") if not is_curve else None,
            values=data.get("values", []) if is_curve else [],
            schema_version=data.get("schema_version", "1"),
            unit=data.get("unit", ""),
            higher_is_better=data.get("higher_is_better", True),
            labels=data.get("labels", {}),
            metadata=data.get("metadata", {}),
            timestamp=data.get("timestamp", ""),
            run_id=data.get("run_id", ""),
            is_curve=is_curve,
            x_unit=data.get("x_unit", ""),
            x_help=data.get("x_help", ""),
            y_unit=data.get("y_unit", ""),
            y_help=data.get("y_help", ""),
        )


@dataclass
class RegressionFinding:
    """Individual regression test finding."""

    kpi_id: str
    baseline_value: float
    current_value: float
    relative_change: float
    change_percent: float
    is_regression: bool
    higher_is_better: bool
    unit: str = ""
    baseline_labels: dict[str, str] = field(default_factory=dict)
    current_labels: dict[str, str] = field(default_factory=dict)
    threshold_used: float = 0.1

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegressionFinding:
        """Create RegressionFinding from dictionary data."""
        return cls(**data)


@dataclass
class KpiCatalogEntry:
    """KPI catalog entry for plugin metadata."""

    kpi_id: str
    name: str = ""
    unit: str = ""
    higher_is_better: bool = True
    is_curve: bool = False
    help: str = ""
    x_unit: str = ""
    x_help: str = ""
    y_unit: str = ""
    y_help: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KpiCatalogEntry:
        """Create KpiCatalogEntry from dictionary data."""
        return cls(**data)


@dataclass
class TestMetadata:
    """Test metadata structure for hierarchical format."""

    timestamp: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestMetadata:
        """Create TestMetadata from dictionary data."""

        return cls(
            timestamp=data.get("timestamp", ""),
            run_id=data.get("run_id", ""),
        )


@dataclass
class HierarchicalTestEntry:
    """Test entry in hierarchical KPI format."""

    run_id: str
    labels: dict[str, str] = field(default_factory=dict)
    metadata: TestMetadata = field(default_factory=TestMetadata)
    kpis: list[HierarchicalKpi] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "run_id": self.run_id,
            "labels": self.labels,
            "metadata": self.metadata.to_dict()
            if isinstance(self.metadata, TestMetadata)
            else self.metadata,
            "kpis": [kpi.to_dict() for kpi in self.kpis],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HierarchicalTestEntry:
        """Create HierarchicalTestEntry from dictionary data."""
        # Convert metadata if it's a dict
        metadata_data = data.get("metadata", {})
        if isinstance(metadata_data, dict):
            metadata = TestMetadata.from_dict(metadata_data)
        else:
            metadata = metadata_data

        # Convert kpis if they're dicts
        kpis_data = data.get("kpis", [])
        kpis = [
            HierarchicalKpi.from_dict(kpi) if isinstance(kpi, dict) else kpi for kpi in kpis_data
        ]

        return cls(
            run_id=data["run_id"],
            labels=data.get("labels", {}),
            metadata=metadata,
            kpis=kpis,
        )


@dataclass
class HierarchicalKpiFormat:
    """Hierarchical KPI format structure (schema version 2)."""

    schema_version: str = "2"
    tests: list[HierarchicalTestEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "schema_version": self.schema_version,
            "tests": [test.to_dict() for test in self.tests],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HierarchicalKpiFormat:
        """Create HierarchicalKpiFormat from dictionary data."""
        tests_data = data.get("tests", [])
        tests = [
            HierarchicalTestEntry.from_dict(test) if isinstance(test, dict) else test
            for test in tests_data
        ]
        return cls(
            schema_version=data.get("schema_version", "2"),
            tests=tests,
        )


@dataclass
class CaliperTestMetadata:
    """Caliper test metadata structure for __caliper_test_metadata__.yaml files."""

    version: str
    labels: dict[str, str]
    kpi_labels: dict[str, str] | None = None
    mlflow_destination: dict[str, str] | None = None
    timing: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML serialization."""
        result = {
            "version": self.version,
            "labels": self.labels,
        }
        if self.kpi_labels is not None:
            result["kpi_labels"] = self.kpi_labels
        if self.mlflow_destination is not None:
            result["mlflow_destination"] = self.mlflow_destination
        if self.timing is not None:
            result["timing"] = self.timing
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaliperTestMetadata:
        """Create CaliperTestMetadata from dictionary data."""
        return cls(
            version=data["version"],
            labels=data["labels"],
            kpi_labels=data.get("kpi_labels"),
            mlflow_destination=data.get("mlflow_destination"),
            timing=data.get("timing"),
        )


# Export all public classes
__all__ = [
    "HierarchicalKpi",
    "KpiRecord",
    "RegressionFinding",
    "KpiCatalogEntry",
    "TestMetadata",
    "HierarchicalTestEntry",
    "HierarchicalKpiFormat",
    "CaliperTestMetadata",
]
