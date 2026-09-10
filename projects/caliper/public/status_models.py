"""
Unified status models for Caliper commands.

This module provides type-safe dataclass models for status objects returned by
various Caliper commands. These models are used by both the engine (to produce
status) and orchestration (to consume status).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import yaml


class StatusLevel(StrEnum):
    """Status level enumeration for all Caliper commands."""

    SUCCESS = "success"
    FAILED = "failed"
    WARNING = "warning"
    DISABLED = "disabled"
    REGRESSION_DETECTED = "regression_detected"


yaml.add_representer(
    StatusLevel, lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", str(data))
)


@dataclass
class BaseStatus:
    """Base status class with common fields for all Caliper commands."""

    status: StatusLevel
    completed_at: float
    log_file: str | None = None
    error: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {}
        for field_name, field_value in self.__dict__.items():
            result[field_name] = field_value
        return result


@dataclass
class ParseStatus(BaseStatus):
    """Status model for caliper parse command."""

    success: bool = False
    exit_code: int = 0
    detail: str | None = None
    plugin_module: str | None = None
    record_count: int | None = None
    parse_cache_ref: str | None = None


@dataclass
class VisualizeStatus(BaseStatus):
    """Status model for caliper visualize command."""

    success: bool = False
    exit_code: int = 0
    detail: str | None = None
    plugin_module: str | None = None
    output_files: list[str] = field(default_factory=list)
    output_dir: str | None = None
    generated_files: int = 0


@dataclass
class KpiGenerateStatus(BaseStatus):
    """Status model for caliper kpi generate command."""

    output_file: str | None = None
    kpi_count: int | None = None
    plugin_module: str | None = None
    html_file: str | None = None

    @property
    def success(self) -> bool:
        """Convenience property for backward compatibility."""
        return self.status == StatusLevel.SUCCESS


@dataclass
class KpiAnalysisStatus(BaseStatus):
    """Status model for caliper kpi analyse-kpis command."""

    success: bool = False
    exit_code: int = 0
    output_file: str | None = None
    regressions_detected: bool = False
    html_file: str | None = None

    # Additional analysis-specific fields
    regression_count: int | None = None
    total_kpis: int | None = None
    baseline_files_count: int | None = None


@dataclass
class AiDataExportStatus(BaseStatus):
    """Status model for caliper ai-eval-export command."""

    output_file: str | None = None
    ai_data_dir: str | None = None
    payload_size: int | None = None

    @property
    def success(self) -> bool:
        """Convenience property for backward compatibility."""
        return self.status == StatusLevel.SUCCESS


@dataclass
class S3ImportStatus(BaseStatus):
    """Status model for caliper s3 import command."""

    success: bool = False
    exit_code: int = 0
    detail: str | None = None
    output_dir: str | None = None
    file_count: int = 0
    imported_path: str | None = None


@dataclass
class S3ExportStatus(BaseStatus):
    """Status model for caliper s3 export command."""

    success: bool = False
    exit_code: int = 0
    detail: str | None = None
    exported_path: str | None = None
    uploaded_files: int = 0
    total_files: int = 0


@dataclass
class CsvExportStatus(BaseStatus):
    """Status model for caliper kpi csv-export command."""

    output_file: str | None = None
    kpi_count: int = 0

    @property
    def success(self) -> bool:
        """Convenience property for backward compatibility."""
        return self.status == StatusLevel.SUCCESS


# Helper functions for creating status objects


def create_success_status(status_cls: type[BaseStatus], **kwargs: Any) -> BaseStatus:
    """Create a success status object with common fields populated."""
    import time

    defaults = {
        "status": StatusLevel.SUCCESS,
        "completed_at": time.time(),
    }

    # Set success=True for status classes that have it
    if hasattr(status_cls, "__dataclass_fields__") and "success" in status_cls.__dataclass_fields__:
        defaults["success"] = True

    # Set exit_code=0 for CLI status classes
    if (
        hasattr(status_cls, "__dataclass_fields__")
        and "exit_code" in status_cls.__dataclass_fields__
    ):
        defaults["exit_code"] = 0

    return status_cls(**{**defaults, **kwargs})


def create_failure_status(
    status_cls: type[BaseStatus], error: str, exit_code: int = 1, **kwargs: Any
) -> BaseStatus:
    """Create a failure status object with common fields populated."""
    import time

    defaults = {
        "status": StatusLevel.FAILED,
        "error": error,
        "completed_at": time.time(),
    }

    # Set success=False for status classes that have it
    if hasattr(status_cls, "__dataclass_fields__") and "success" in status_cls.__dataclass_fields__:
        defaults["success"] = False

    # Set exit_code for CLI status classes
    if (
        hasattr(status_cls, "__dataclass_fields__")
        and "exit_code" in status_cls.__dataclass_fields__
    ):
        defaults["exit_code"] = exit_code

    return status_cls(**{**defaults, **kwargs})


def create_disabled_status(status_cls: type[BaseStatus], reason: str, **kwargs: Any) -> BaseStatus:
    """Create a disabled status object."""
    import time

    return status_cls(
        status=StatusLevel.DISABLED, message=reason, completed_at=time.time(), **kwargs
    )
