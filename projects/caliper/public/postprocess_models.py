"""
Dataclass models for Caliper postprocess status.

These models are used for generating and parsing postprocess_status.yaml files
for notifications and status reporting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class FinalPostprocessStatus(StrEnum):
    """Final postprocess status levels."""

    SUCCESS = "success"
    TEST_FAILED = "test_failed"
    PARSE_VISUALIZE_FAILED = "parse_visualize_failed"
    KPI_PIPELINE_FAILED = "kpi_pipeline_failed"
    PERFORMANCE_REGRESSION = "performance_regression"
    PERFORMANCE_INCREASE = "performance_increase"


class PostprocessTestPhase(StrEnum):
    """Test phase outcomes."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class StepStatus(StrEnum):
    """Individual step status levels."""

    SUCCESS = "success"
    FAILED = "failed"
    WARNING = "warning"
    DISABLED = "disabled"
    SKIPPED = "skipped"
    REGRESSION_DETECTED = "regression_detected"


class PostprocessOutcome(StrEnum):
    """Post-processing health, independent of the test-phase outcome."""

    SUCCESS = "success"
    WARNING = "warning"
    POSTPROCESS_FAILURE = "postprocess_failure"
    PERFORMANCE_REGRESSION = "performance_regression"
    PERFORMANCE_INCREASE = "performance_increase"


@dataclass(frozen=True)
class PostprocessClassification:
    """Post-processing health classification, separated from the test-phase outcome."""

    outcome: PostprocessOutcome
    test_failed: bool


def _any_step_status(steps: Any, status: str) -> bool:
    """Check whether any post-processing step has the given status.

    Handles both the current ``steps`` format (list of ``{step_name: step_data}``)
    and the legacy dict format.
    """
    if isinstance(steps, Mapping):
        return any(
            step_data.get("status") == status
            for step_data in steps.values()
            if isinstance(step_data, Mapping)
        )
    if isinstance(steps, list):
        return any(
            step_data.get("status") == status
            for step in steps
            if isinstance(step, Mapping)
            for step_data in step.values()
            if isinstance(step_data, Mapping)
        )
    return False


def classify_postprocess_outcome(status_data: Mapping[str, Any]) -> PostprocessClassification:
    """Classify post-processing health from a raw postprocess status dict.

    Answers "did the post-processing itself fail?" independently of the test-phase
    outcome. ``final_status`` gives ``test_failed`` top priority, so it masks real
    post-processing step failures — the steps are therefore inspected first.

    Tolerates missing keys so it can be used directly on ``yaml.safe_load`` results.

    Args:
        status_data: Raw postprocess status dict (or ``PostprocessStatus.to_dict()``).

    Returns:
        Classification with the post-processing outcome and whether the test phase failed.

    Raises:
        ValueError: If ``status_data`` is not a mapping.
    """
    if not isinstance(status_data, Mapping):
        raise ValueError(
            f"postprocess status data must be a mapping, got {type(status_data).__name__}"
        )

    final_status = status_data.get("final_status")
    success = status_data.get("success")

    test_phase = status_data.get("test_phase")
    phase = test_phase.get("phase") if isinstance(test_phase, Mapping) else None
    if phase:
        test_failed = phase == PostprocessTestPhase.FAILED
    else:
        test_failed = final_status == FinalPostprocessStatus.TEST_FAILED

    steps = status_data.get("steps", [])
    has_failed_steps = _any_step_status(steps, "failed")
    has_warning_steps = _any_step_status(steps, "warning")

    # Step failures first: a "test_failed" label masks real post-processing failures.
    if has_failed_steps:
        return PostprocessClassification(PostprocessOutcome.POSTPROCESS_FAILURE, test_failed)

    # Warnings-only: the writer sets success="warning" only when no step actually failed.
    if success == "warning" or (test_failed and has_warning_steps):
        return PostprocessClassification(PostprocessOutcome.WARNING, test_failed)

    if final_status in (
        FinalPostprocessStatus.PARSE_VISUALIZE_FAILED,
        FinalPostprocessStatus.KPI_PIPELINE_FAILED,
    ):
        return PostprocessClassification(PostprocessOutcome.POSTPROCESS_FAILURE, test_failed)

    if final_status == FinalPostprocessStatus.PERFORMANCE_REGRESSION:
        return PostprocessClassification(PostprocessOutcome.PERFORMANCE_REGRESSION, test_failed)

    if final_status == FinalPostprocessStatus.PERFORMANCE_INCREASE:
        return PostprocessClassification(PostprocessOutcome.PERFORMANCE_INCREASE, test_failed)

    if (
        final_status in (FinalPostprocessStatus.SUCCESS, FinalPostprocessStatus.TEST_FAILED)
        or success is True
    ):
        return PostprocessClassification(PostprocessOutcome.SUCCESS, test_failed)

    # Unknown final_status with a falsy success field: conservatively a failure.
    return PostprocessClassification(PostprocessOutcome.POSTPROCESS_FAILURE, test_failed)


@dataclass
class PostprocessTestPhaseInfo:
    """Test phase information."""

    phase: PostprocessTestPhase
    message: str | None = None


@dataclass
class BaseStepResult:
    """Base class for step results."""

    status: StepStatus
    completed_at: float
    log_file: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {}
        for field_name, field_value in self.__dict__.items():
            if field_value is not None:
                if isinstance(field_value, StepStatus):
                    result[field_name] = field_value.value
                else:
                    result[field_name] = field_value
        return result


@dataclass
class ParseStepResult(BaseStepResult):
    """Parse step result."""

    plugin_module: str | None = None
    record_count: int | None = None
    parse_cache_ref: str | None = None
    detail: str | None = None
    exit_code: int | None = None


@dataclass
class VisualizeStepResult(BaseStepResult):
    """Visualize step result."""

    plugin_module: str | None = None
    output_files: list[dict[str, str]] = field(default_factory=list)
    output_dir: str | None = None
    generated_files: int = 0
    error: str | None = None
    message: str | None = None
    exit_code: int | None = None


@dataclass
class KpiGenerateStepResult(BaseStepResult):
    """KPI generation step result."""

    output_file: str | None = None
    error: str | None = None
    html_file: str | None = None


@dataclass
class KpiAnalysisStepResult(BaseStepResult):
    """KPI analysis step result."""

    success: bool | None = None
    exit_code: int | None = None
    output_file: str | None = None
    regressions_detected: bool | None = None
    regression_count: int | None = None
    total_kpis: int | None = None
    error: str | None = None
    message: str | None = None
    html_file: str | None = None


@dataclass
class AiDataStepResult(BaseStepResult):
    """AI data export step result."""

    output_file: str | None = None
    ai_data_dir: str | None = None
    error: str | None = None


@dataclass
class S3StepResult(BaseStepResult):
    """S3 import/export step result."""

    success: bool | None = None
    exit_code: int | None = None
    detail: str | None = None
    output_dir: str | None = None
    file_count: int | None = None
    uploaded_files: int | None = None
    total_files: int | None = None
    exported_path: str | None = None
    imported_path: str | None = None
    error: str | None = None


@dataclass
class CsvExportStepResult(BaseStepResult):
    """CSV export step result."""

    output_file: str | None = None
    kpi_count: int | None = None
    error: str | None = None


@dataclass
class PostprocessStatus:
    """Complete postprocess status for YAML generation and parsing."""

    final_status: FinalPostprocessStatus
    success: bool | str  # Can be bool or "warning"
    base_directory: str
    test_phase: PostprocessTestPhaseInfo
    steps: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_orchestration_result(cls, result: dict[str, Any]) -> PostprocessStatus:
        """Create PostprocessStatus from orchestration result dict."""

        # Parse test phase
        test_phase_data = result.get("test_phase", {})
        test_phase = PostprocessTestPhaseInfo(
            phase=PostprocessTestPhase(test_phase_data.get("phase", "NOT_AVAILABLE")),
            message=test_phase_data.get("message"),
        )

        return cls(
            final_status=FinalPostprocessStatus(result["final_status"]),
            success=result["success"],
            base_directory=result["base_directory"],
            test_phase=test_phase,
            steps=result.get("steps", []),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML serialization."""
        return {
            "final_status": self.final_status.value,
            "success": self.success,
            "base_directory": self.base_directory,
            "test_phase": {
                "phase": self.test_phase.phase.value,
                "message": self.test_phase.message,
            },
            "steps": self.steps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostprocessStatus:
        """Parse from dictionary (loaded from YAML)."""

        # Parse test phase
        test_phase_data = data.get("test_phase", {})
        test_phase = PostprocessTestPhaseInfo(
            phase=PostprocessTestPhase(test_phase_data.get("phase", "NOT_AVAILABLE")),
            message=test_phase_data.get("message"),
        )

        return cls(
            final_status=FinalPostprocessStatus(data["final_status"]),
            success=data["success"],
            base_directory=data["base_directory"],
            test_phase=test_phase,
            steps=data.get("steps", []),
        )

    def get_step_result(self, step_name: str) -> dict[str, Any] | None:
        """Get result for a specific step."""
        for step in self.steps:
            if step_name in step:
                return step[step_name]
        return None

    def has_regressions(self) -> bool:
        """Check if any step detected regressions."""
        analyse_step = self.get_step_result("analyse_kpis")
        if analyse_step:
            return analyse_step.get("regressions_detected", False)
        return False

    def is_success(self) -> bool:
        """Check if the overall postprocess was successful."""
        return self.final_status == FinalPostprocessStatus.SUCCESS

    def is_failure(self) -> bool:
        """Check if the postprocess failed."""
        return not self.is_success() and self.success is not True

    def has_failed_steps(self) -> bool:
        """Check whether any post-processing step failed."""
        return _any_step_status(self.steps, "failed")

    def is_test_failure(self) -> bool:
        """Check whether the test phase failed (as opposed to the post-processing)."""
        return (
            self.test_phase.phase == PostprocessTestPhase.FAILED
            or self.final_status == FinalPostprocessStatus.TEST_FAILED
        )

    def is_postprocess_failure(self) -> bool:
        """Check whether the post-processing itself failed, independently of the test phase."""
        return self.classify().outcome == PostprocessOutcome.POSTPROCESS_FAILURE

    def classify(self) -> PostprocessClassification:
        """Classify post-processing health independently of the test-phase outcome."""
        return classify_postprocess_outcome(self.to_dict())

    def get_failure_reason(self) -> str | None:
        """Get human-readable failure reason."""
        if self.is_success():
            return None

        match self.final_status:
            case FinalPostprocessStatus.TEST_FAILED:
                return f"Test phase failed: {self.test_phase.message}"
            case FinalPostprocessStatus.PARSE_VISUALIZE_FAILED:
                details = self._get_failed_step_details("parse", "visualize")
                if details:
                    return f"Parse or visualize step failed: {details}"
                return "Parse or visualize step failed"
            case FinalPostprocessStatus.KPI_PIPELINE_FAILED:
                details = self._get_failed_step_details(
                    "artifacts_to_kpis",
                    "artifacts_to_ai_data",
                    "s3_import",
                    "s3_export",
                    "analyse_kpis",
                )
                if details:
                    return f"KPI pipeline failed: {details}"
                return "KPI pipeline failed"
            case FinalPostprocessStatus.PERFORMANCE_REGRESSION:
                return "Performance regression detected"
            case FinalPostprocessStatus.PERFORMANCE_INCREASE:
                return "Performance improvement detected"
            case _:
                return f"Unknown failure: {self.final_status}"

    def _get_failed_step_details(self, *step_names: str) -> str | None:
        """Extract error details from failed steps."""
        failed_details = []
        for name in step_names:
            step = self.get_step_result(name)
            if step and step.get("status") == "failed":
                detail = step.get("error") or step.get("detail") or step.get("reason")
                if detail:
                    failed_details.append(f"{name}: {detail}")
                else:
                    failed_details.append(name)
        return "; ".join(failed_details) if failed_details else None


# Helper functions for creating status objects


def create_postprocess_status(
    final_status: FinalPostprocessStatus,
    success: bool | str,
    base_directory: str,
    test_phase: PostprocessTestPhaseInfo,
    steps: list[dict[str, Any]] | None = None,
) -> PostprocessStatus:
    """Create a PostprocessStatus object."""
    return PostprocessStatus(
        final_status=final_status,
        success=success,
        base_directory=base_directory,
        test_phase=test_phase,
        steps=steps or [],
    )


def create_test_phase_info(
    phase: PostprocessTestPhase,
    message: str | None = None,
) -> PostprocessTestPhaseInfo:
    """Create a PostprocessTestPhaseInfo object."""
    return PostprocessTestPhaseInfo(phase=phase, message=message)


# YAML I/O functions


def save_postprocess_status_yaml(status: PostprocessStatus, file_path: Path) -> None:
    """Save PostprocessStatus to YAML file."""
    import yaml

    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(status.to_dict(), f, default_flow_style=False, sort_keys=True)


def load_postprocess_status_yaml(file_path: Path) -> PostprocessStatus:
    """Load PostprocessStatus from YAML file."""
    import yaml

    with open(file_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return PostprocessStatus.from_dict(data)
