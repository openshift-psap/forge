"""Regression analysis report dataclasses.

Contains all dataclass definitions specifically for KPI regression analysis reports,
separated from the core KPI dataclasses for better organization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class OverallStatus(StrEnum):
    """Overall analysis status."""

    PASS = "PASS"
    REGRESSION_DETECTED = "REGRESSION_DETECTED"
    NO_BASELINE = "NO_BASELINE"
    NO_TEST_PERFORMED = "NO_TEST_PERFORMED"


class Algorithm(StrEnum):
    """Regression testing algorithms."""

    SCALAR_RELATIVE_CHANGE = "SCALAR_RELATIVE_CHANGE"
    CURVE_AUC_CHANGE = "CURVE_AUC_CHANGE"


class Verdict(StrEnum):
    """Individual KPI test verdict."""

    PASS = "PASS"
    REGRESSION = "REGRESSION"
    SKIPPED = "SKIPPED"


@dataclass
class CurrentValueInfo:
    """Structured current_value field for RegressionTestResult and ResultEntry."""

    value: Any = None  # For scalar KPIs
    values: list[list[float]] | None = None  # For curve KPIs (list of [x, y] coordinate pairs)
    comparison_keys: dict[str, Any] = field(default_factory=dict)  # Updated to Any for flexibility

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)

        # Filter fields based on what has data - include only the relevant value field
        if self.values is not None:
            # Curve KPI: include 'values', remove 'value'
            result.pop("value", None)
        else:
            # Scalar KPI: include 'value', remove 'values'
            result.pop("values", None)

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CurrentValueInfo:
        """Create CurrentValueInfo from dictionary data."""
        return cls(**data)


@dataclass
class ReportMetadata:
    """Analysis report metadata."""

    total_tested: int = 0
    total_skipped: int = 0
    plugin_module: str = ""
    caliper_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReportMetadata:
        """Create ReportMetadata from dictionary data."""
        return cls(**data)


@dataclass
class ResultLabels:
    """Labels section for result entry."""

    comparison_keys: dict[str, Any] = field(default_factory=dict)
    distinct_keys: dict[str, Any] = field(default_factory=dict)
    ignore_keys: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResultLabels:
        """Create ResultLabels from dictionary data."""
        return cls(**data)


@dataclass
class ResultEntry:
    """Individual result entry in the results array."""

    kpi_id: str
    verdict: Verdict
    labels: ResultLabels = field(default_factory=ResultLabels)
    run_id: str = ""
    is_curve: bool = False
    higher_is_better: bool = True
    current_value: CurrentValueInfo = field(default_factory=CurrentValueInfo)
    baseline_values: list[dict[str, Any]] = field(default_factory=list)
    baseline_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)
        # Handle nested dataclass serialization
        if isinstance(self.labels, ResultLabels):
            result["labels"] = self.labels.to_dict()
        if isinstance(self.current_value, CurrentValueInfo):
            result["current_value"] = self.current_value.to_dict()
        if self.baseline_values:
            result["baseline_values"] = self.baseline_values
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResultEntry:
        """Create ResultEntry from dictionary data."""
        converted_data = data.copy()

        if "verdict" in converted_data:
            converted_data["verdict"] = Verdict(converted_data["verdict"])

        if "labels" in converted_data and isinstance(converted_data["labels"], dict):
            converted_data["labels"] = ResultLabels.from_dict(converted_data["labels"])

        if "current_value" in converted_data and isinstance(converted_data["current_value"], dict):
            converted_data["current_value"] = CurrentValueInfo.from_dict(
                converted_data["current_value"]
            )

        return cls(**converted_data)


@dataclass
class AnalysisSection:
    """Analysis section of the regression report."""

    status: OverallStatus
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalysisSection:
        """Create AnalysisSection from dictionary data."""
        return cls(
            status=OverallStatus(data["status"]),
            timestamp=data["timestamp"],
        )


@dataclass
class TestedSection:
    """Tested section of the regression report."""

    total_kpis: int
    pass_count: int
    regression: int
    skipped: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestedSection:
        """Create TestedSection from dictionary data."""
        return cls(**data)


@dataclass
class OverallSection:
    """Overall section of the regression report."""

    verdict: OverallStatus
    regression_count: int
    total_tested: int
    total_skipped: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverallSection:
        """Create OverallSection from dictionary data."""
        return cls(
            verdict=OverallStatus(data["verdict"]),
            regression_count=data["regression_count"],
            total_tested=data["total_tested"],
            total_skipped=data["total_skipped"],
        )


@dataclass
class InputDataSection:
    """Input data section of the regression report."""

    current_source: dict[str, Any]
    baseline_sources: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InputDataSection:
        """Create InputDataSection from dictionary data."""
        return cls(**data)


@dataclass
class RegressionReport:
    """Main regression analysis report structure."""

    analysis: AnalysisSection
    config: dict[str, Any]
    tested: TestedSection
    overall: OverallSection
    input_data: InputDataSection
    results: list[ResultEntry]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "analysis": self.analysis.to_dict(),
            "config": self.config,
            "tested": self.tested.to_dict(),
            "overall": self.overall.to_dict(),
            "input_data": self.input_data.to_dict(),
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegressionReport:
        """Create RegressionReport from dictionary data."""
        # Convert nested sections
        analysis = AnalysisSection.from_dict(data["analysis"])
        tested = TestedSection.from_dict(data["tested"])
        overall = OverallSection.from_dict(data["overall"])
        input_data = InputDataSection.from_dict(data["input_data"])

        # Convert results list
        results = []
        for result_data in data.get("results", []):
            result = ResultEntry(
                kpi_id=result_data["kpi_id"],
                verdict=Verdict(result_data["verdict"]),
                **{k: v for k, v in result_data.items() if k not in ["kpi_id", "verdict"]},
            )
            results.append(result)

        return cls(
            analysis=analysis,
            config=data["config"],
            tested=tested,
            overall=overall,
            input_data=input_data,
            results=results,
        )

    def is_successful(self) -> bool:
        """Check if analysis completed successfully."""
        return self.analysis.status in (OverallStatus.PASS, OverallStatus.NO_BASELINE)


@dataclass
class RegressionTestResult:
    """Result of running a single KPI regression test."""

    kpi_id: str
    verdict: Verdict
    labels: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    is_curve: bool = False
    higher_is_better: bool = True
    current_value: CurrentValueInfo | None = None
    baseline_values: list[dict[str, Any]] = field(default_factory=list)
    baseline_count: int = 0
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)
        if self.current_value:
            result["current_value"] = self.current_value.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegressionTestResult:
        """Create RegressionTestResult from dictionary data."""
        converted_data = data.copy()

        if "verdict" in converted_data:
            converted_data["verdict"] = Verdict(converted_data["verdict"])

        if "current_value" in converted_data:
            current_value = converted_data["current_value"]
            if isinstance(current_value, dict):
                current_value = CurrentValueInfo.from_dict(current_value)
            converted_data["current_value"] = current_value

        return cls(**converted_data)


@dataclass
class KpiComputationStatus:
    """Status result from KPI computation operations."""

    status: str  # "success", "failed", "warning"
    success: bool
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    tests_processed: int = 0
    total_tests: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KpiComputationStatus:
        """Create KpiComputationStatus from dictionary data."""
        return cls(**data)

    @classmethod
    def success_status(
        cls, tests_processed: int, total_tests: int | None = None
    ) -> KpiComputationStatus:
        """Create a success status."""
        return cls(
            status="success",
            success=True,
            tests_processed=tests_processed,
            total_tests=total_tests or tests_processed,
        )

    @classmethod
    def failure_status(
        cls, message: str, tests_processed: int = 0, total_tests: int = 0
    ) -> KpiComputationStatus:
        """Create a failure status."""
        return cls(
            status="failed",
            success=False,
            message=message,
            tests_processed=tests_processed,
            total_tests=total_tests,
        )


@dataclass
class MlflowConversionResult:
    """Result of converting KPIs to MLflow format."""

    status: str
    tests_processed: int = 0
    total_tests: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    reason: str | None = None
    partial: bool = False
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MlflowConversionResult:
        """Create MlflowConversionResult from dictionary data."""
        return cls(**data)

    def is_success(self) -> bool:
        """Check if the conversion was successful (including partial success)."""
        return self.status == "success"

    def is_failure(self) -> bool:
        """Check if the conversion failed."""
        return self.status == "failed"

    def is_skipped(self) -> bool:
        """Check if the conversion was skipped."""
        return self.status == "skipped"

    def to_status_data(self, **extra_fields) -> dict[str, Any]:
        """Convert to status data format for CLI YAML files.

        Adds legacy 'success' field and allows extra fields to be merged in.
        """
        result = self.to_dict()
        result["success"] = self.is_success()
        result.update(extra_fields)
        return result


# Analysis summary dataclasses for structured reporting
@dataclass
class TestSummary:
    """Test summary statistics."""

    total_kpis: int
    pass_count: int
    regression_count: int
    skipped_count: int
    improvement_count: int = 0


@dataclass
class ConfigSummary:
    """Configuration summary."""

    comparison_labels: list[str]
    ignored_labels: list[str]
    sorting_labels: list[str]
    regression_config: dict[str, Any]


@dataclass
class BaselineSummary:
    """Baseline data summary."""

    relevant_sources: list[dict[str, Any]]
    irrelevant_sources: list[dict[str, Any]]
    baseline_source_count: int
    baseline_skipped: dict[str, int]
    current_source: dict[str, Any]


@dataclass
class AnalysisSummary:
    """High-level analysis summary."""

    tested: TestSummary
    config: ConfigSummary
    baseline_info: BaselineSummary
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalysisSummary:
        """Create AnalysisSummary from dictionary data."""
        converted_data = data.copy()

        if "tested" in converted_data:
            converted_data["tested"] = TestSummary(**converted_data["tested"])

        if "config" in converted_data:
            converted_data["config"] = ConfigSummary(**converted_data["config"])

        if "baseline_info" in converted_data:
            converted_data["baseline_info"] = BaselineSummary(**converted_data["baseline_info"])

        return cls(**converted_data)
