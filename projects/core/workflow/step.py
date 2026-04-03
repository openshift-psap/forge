"""Workflow step protocol and result types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import WorkflowContext


class StepStatus(Enum):
    """Step execution status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    """
    Result of a single step execution.

    Attributes:
        success: Whether the step completed successfully
        message: Human-readable status message
        error: Exception if step failed
        artifacts: Paths to artifacts produced by this step
        data: Arbitrary output data for downstream steps
        duration_seconds: Execution time in seconds
    """

    success: bool
    message: str = ""
    error: Exception | None = None
    artifacts: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    start_time: datetime | None = None
    end_time: datetime | None = None

    @classmethod
    def ok(cls, message: str = "Success", **data: Any) -> "StepResult":
        """Create a successful result."""
        return cls(success=True, message=message, data=data)

    @classmethod
    def fail(cls, message: str, error: Exception | None = None) -> "StepResult":
        """Create a failed result."""
        return cls(success=False, message=message, error=error)


class WorkflowStep(ABC):
    """
    Abstract base class for workflow steps.

    Implement execute() to define step behavior.
    Step name defaults to class name if not provided.
    """

    def __init__(self, name: str | None = None):
        """
        Initialize step.

        Args:
            name: Optional step name (defaults to class name)
        """
        self._name = name

    @property
    def name(self) -> str:
        """Get step name."""
        if self._name:
            return self._name
        # Default to class name, converting CamelCase to snake_case
        class_name = self.__class__.__name__
        # Remove 'Step' suffix if present
        if class_name.endswith("Step"):
            class_name = class_name[:-4]
        # Convert to lowercase
        return class_name.lower()

    @abstractmethod
    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """
        Execute the step.

        Args:
            ctx: Workflow execution context

        Returns:
            StepResult indicating success/failure and any outputs
        """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
