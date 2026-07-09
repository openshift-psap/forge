"""Aggregated post-processing outcome after FORGE test phase + Caliper steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Ordered worst-first for documentation; see compute_final_postprocess_status().
FINAL_TEST_FAILED = "test_failed"
FINAL_PARSE_VISUALIZE_FAILED = "parse_visualize_failed"
FINAL_KPI_PIPELINE_FAILED = "kpi_pipeline_failed"
FINAL_PERFORMANCE_REGRESSION = "performance_regression"
FINAL_PERFORMANCE_INCREASE = "performance_increase"
FINAL_SUCCESS = "success"

FinalPostprocessStatus = Literal[
    "success",
    "test_failed",
    "parse_visualize_failed",
    "kpi_pipeline_failed",
    "performance_increase",
    "performance_regression",
]


@dataclass(frozen=True)
class TestPhaseOutcome:
    """Result of the orchestration test phase (before Caliper post-processing)."""

    phase: Literal["SUCCESS", "FAILED", "NOT_AVAILABLE"]
    message: str | None = None


def compute_final_postprocess_status(
    *,
    test_outcome: TestPhaseOutcome,
    parse_failed: bool,
    visualize_failed: bool,
    artifacts_to_kpis_failed: bool,
    ai_data_failed: bool,
    s3_import_failed: bool,
    s3_export_failed: bool,
    analyze_failed: bool,
    has_regression: bool,
    has_improvement: bool,
) -> FinalPostprocessStatus:
    """
    Single authoritative label for CI / dashboards.

    Priority (first match wins): test failure → parse/viz failure → KPI/analyze/ai_data/s3_import/s3_export pipeline failure →
    regression → improvement → success.
    """
    if test_outcome.phase == "FAILED":
        return FINAL_TEST_FAILED
    if parse_failed or visualize_failed:
        return FINAL_PARSE_VISUALIZE_FAILED
    if (
        artifacts_to_kpis_failed
        or ai_data_failed
        or s3_import_failed
        or s3_export_failed
        or analyze_failed
    ):
        return FINAL_KPI_PIPELINE_FAILED
    if has_regression:
        return FINAL_PERFORMANCE_REGRESSION
    if has_improvement:
        return FINAL_PERFORMANCE_INCREASE
    return FINAL_SUCCESS
