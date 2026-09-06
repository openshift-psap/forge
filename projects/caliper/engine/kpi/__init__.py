"""KPI generation, OpenSearch, regression."""

from .dataclasses import (
    KpiCatalogEntry,
    KpiRecord,
    RegressionFinding,
)
from .decorators import (
    Curve,
    Format,
    HigherBetter,
    KPIMetadata,
    LowerBetter,
    TestLabelExtractor,
    build_catalog_from_functions,
    create_label_extractor,
    get_kpi_functions,
    is_curve_kpi,
)
from .report_dataclasses import (
    AnalysisSummary,
    BaselineSummary,
    ConfigSummary,
    KpiComputationStatus,
    OverallStatus,
    RegressionReport,
    TestSummary,
)

__all__ = [
    # Core dataclasses - plugins should use these
    "KpiRecord",
    "KpiCatalogEntry",
    "RegressionFinding",
    "RegressionReport",
    # Status dataclasses
    "KpiComputationStatus",
    "OverallStatus",
    # Summary dataclasses
    "AnalysisSummary",
    "TestSummary",
    "ConfigSummary",
    "BaselineSummary",
    # KPI function decorators and utilities
    "Curve",
    "Format",
    "HigherBetter",
    "KPIMetadata",
    "LowerBetter",
    "TestLabelExtractor",
    "build_catalog_from_functions",
    "create_label_extractor",
    "get_kpi_functions",
    "is_curve_kpi",
]
