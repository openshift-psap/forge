"""
Postprocess status enumeration for distinguishing different types of outcomes.
"""

from enum import Enum


class PostprocessStatus(Enum):
    """Enumeration of postprocess status outcomes."""

    SUCCESS = "success"
    CRASH = "crash"
    PERFORMANCE_REGRESSION = "performance_regression"
    PARSE_FAILED = "parse_failed"
    VISUALIZE_FAILED = "visualize_failed"
    KPI_FAILED = "kpi_failed"
    ANALYZE_FAILED = "analyze_failed"

    def __str__(self) -> str:
        return self.value

    @property
    def is_success(self) -> bool:
        """Check if this status represents a successful outcome."""
        return self == PostprocessStatus.SUCCESS

    @property
    def is_failure(self) -> bool:
        """Check if this status represents a failure outcome."""
        return self != PostprocessStatus.SUCCESS
