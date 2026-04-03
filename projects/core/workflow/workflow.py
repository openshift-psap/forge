"""Base workflow class with step registration."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .step import StepResult, WorkflowStep

if TYPE_CHECKING:
    from .context import WorkflowContext


@dataclass
class WorkflowResult:
    """
    Result of a complete workflow execution.

    Attributes:
        success: Whether all steps completed successfully
        step_results: Results from each step, keyed by step name
        failed_step: Name of first step that failed (if any)
        duration_seconds: Total execution time
        run_uuid: UUID of this workflow run
    """

    success: bool
    step_results: dict[str, StepResult] = field(default_factory=dict)
    failed_step: str | None = None
    duration_seconds: float = 0.0
    run_uuid: str = ""
    start_time: datetime | None = None
    end_time: datetime | None = None


class Workflow(ABC):
    """
    Base class for defining workflows with steps and finally blocks.

    Subclasses implement define_steps() to register steps.
    Steps run sequentially; finally steps always run regardless of failure.

    Example:
        class BenchmarkWorkflow(Workflow):
            def define_steps(self):
                self.add_step(DeployVLLMStep(model=..., vllm_image=..., runtime_args=...))
                self.add_step(RunGuideLLMStep(...))
                self.add_finally(CollectArtifactsStep())
                self.add_finally(CleanupDeploymentStep())
    """

    def __init__(self, ctx: "WorkflowContext"):
        """
        Initialize workflow with context.

        Args:
            ctx: Workflow execution context
        """
        self.ctx = ctx
        self._steps: list[WorkflowStep] = []
        self._finally_steps: list[WorkflowStep] = []
        self._defined = False

    def add_step(self, step: WorkflowStep) -> None:
        """
        Add a step to the workflow.

        Steps run in order of registration. If a step fails,
        remaining steps are skipped and finally steps run.

        Args:
            step: WorkflowStep instance to add
        """
        self._steps.append(step)

    def add_finally(self, step: WorkflowStep) -> None:
        """
        Add a finally step that always runs.

        Finally steps run in order after all normal steps complete
        or after a step failure. They run even if previous finally
        steps fail.

        Args:
            step: WorkflowStep instance to add
        """
        self._finally_steps.append(step)

    @abstractmethod
    def define_steps(self) -> None:
        """
        Define workflow steps.

        Override this method to register steps via add_step()
        and add_finally().
        """

    @property
    def steps(self) -> list[WorkflowStep]:
        """Get registered steps."""
        self._ensure_defined()
        return self._steps

    @property
    def finally_steps(self) -> list[WorkflowStep]:
        """Get registered finally steps."""
        self._ensure_defined()
        return self._finally_steps

    def _ensure_defined(self) -> None:
        """Ensure define_steps() has been called."""
        if not self._defined:
            self.define_steps()
            self._defined = True

    def execute(self) -> WorkflowResult:
        """
        Execute the workflow.

        Runs all steps sequentially, then runs finally steps.
        Uses SequentialExecutor internally.

        Returns:
            WorkflowResult with step outcomes
        """
        from .executor import SequentialExecutor

        executor = SequentialExecutor()
        return executor.execute(self)
