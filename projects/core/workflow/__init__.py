"""Forge workflow engine.

A simple, testable workflow engine for sequential step execution
with finally/cleanup blocks. Integrates with the existing DSL patterns.

Example usage:
    from projects.core.workflow import Workflow, WorkflowContext, WorkflowStep, StepResult

    class MyStep(WorkflowStep):
        def execute(self, ctx: WorkflowContext) -> StepResult:
            # Do work...
            return StepResult.ok("Done")

    class MyWorkflow(Workflow):
        def define_steps(self):
            self.add_step(MyStep())
            self.add_finally(CleanupStep())

    ctx = WorkflowContext.from_environment()
    workflow = MyWorkflow(ctx)
    result = workflow.execute()
"""

from .context import WorkflowContext
from .executor import SequentialExecutor
from .step import StepResult, StepStatus, WorkflowStep
from .workflow import Workflow, WorkflowResult

__all__ = [
    "SequentialExecutor",
    "StepResult",
    "StepStatus",
    "Workflow",
    "WorkflowContext",
    "WorkflowResult",
    "WorkflowStep",
]
