"""KPI generation, OpenSearch, regression."""

from .decorators import (
    Format,
    HigherBetter,
    KPIMetadata,
    LowerBetter,
    TestLabelExtractor,
    TwoDimensional,
    build_catalog_from_functions,
    create_label_extractor,
    get_kpi_functions,
    is_2d_kpi,
)

__all__ = [
    "Format",
    "HigherBetter",
    "KPIMetadata",
    "LowerBetter",
    "TestLabelExtractor",
    "TwoDimensional",
    "build_catalog_from_functions",
    "create_label_extractor",
    "get_kpi_functions",
    "is_2d_kpi",
]
