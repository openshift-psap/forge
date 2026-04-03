"""Unit tests for SequentialExecutor."""

import tempfile
from pathlib import Path

import pytest

from projects.core.workflow import (
    SequentialExecutor,
    StepResult,
    Workflow,
    WorkflowContext,
    WorkflowStep,
)


class PassingStep(WorkflowStep):
    """A step that always succeeds."""

    def __init__(self, name: str, record: list[str]):
        super().__init__(name=name)
        self.record = record

    def execute(self, ctx: WorkflowContext) -> StepResult:
        self.record.append(f"executed:{self.name}")
        return StepResult.ok(f"Step {self.name} passed")


class FailingStep(WorkflowStep):
    """A step that always fails."""

    def __init__(self, name: str, record: list[str]):
        super().__init__(name=name)
        self.record = record

    def execute(self, ctx: WorkflowContext) -> StepResult:
        self.record.append(f"executed:{self.name}")
        return StepResult.fail(f"Step {self.name} failed")


class ExceptionStep(WorkflowStep):
    """A step that raises an exception."""

    def __init__(self, name: str, record: list[str]):
        super().__init__(name=name)
        self.record = record

    def execute(self, ctx: WorkflowContext) -> StepResult:
        self.record.append(f"executed:{self.name}")
        raise RuntimeError(f"Step {self.name} exploded")


class TestSequentialExecutor:
    """Tests for SequentialExecutor."""

    @pytest.fixture
    def temp_artifact_dir(self):
        """Create temporary artifact directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        """Create workflow context with temp artifact dir."""
        return WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

    def test_steps_run_in_order(self, context):
        """Steps execute in registration order."""
        record: list[str] = []

        class TestWorkflow(Workflow):
            def define_steps(self):
                self.add_step(PassingStep("first", record))
                self.add_step(PassingStep("second", record))
                self.add_step(PassingStep("third", record))

        workflow = TestWorkflow(context)
        result = workflow.execute()

        assert result.success
        assert record == ["executed:first", "executed:second", "executed:third"]
        assert "first" in result.step_results
        assert "second" in result.step_results
        assert "third" in result.step_results

    def test_finally_runs_on_success(self, context):
        """Finally steps run after successful completion."""
        record: list[str] = []

        class TestWorkflow(Workflow):
            def define_steps(self):
                self.add_step(PassingStep("main", record))
                self.add_finally(PassingStep("cleanup", record))

        workflow = TestWorkflow(context)
        result = workflow.execute()

        assert result.success
        assert record == ["executed:main", "executed:cleanup"]

    def test_finally_runs_on_failure(self, context):
        """Finally steps execute even when normal steps fail."""
        record: list[str] = []

        class TestWorkflow(Workflow):
            def define_steps(self):
                self.add_step(PassingStep("first", record))
                self.add_step(FailingStep("failing", record))
                self.add_step(PassingStep("skipped", record))
                self.add_finally(PassingStep("cleanup1", record))
                self.add_finally(PassingStep("cleanup2", record))

        workflow = TestWorkflow(context)
        result = workflow.execute()

        assert not result.success
        assert result.failed_step == "failing"
        # "skipped" should NOT be in the record
        assert record == [
            "executed:first",
            "executed:failing",
            "executed:cleanup1",
            "executed:cleanup2",
        ]

    def test_finally_runs_on_exception(self, context):
        """Finally steps run even when a step raises an exception."""
        record: list[str] = []

        class TestWorkflow(Workflow):
            def define_steps(self):
                self.add_step(PassingStep("first", record))
                self.add_step(ExceptionStep("exploding", record))
                self.add_step(PassingStep("skipped", record))
                self.add_finally(PassingStep("cleanup", record))

        workflow = TestWorkflow(context)
        result = workflow.execute()

        assert not result.success
        assert result.failed_step == "exploding"
        assert "exploding" in result.step_results
        assert result.step_results["exploding"].error is not None
        assert record == ["executed:first", "executed:exploding", "executed:cleanup"]

    def test_all_finally_steps_run_even_if_one_fails(self, context):
        """All finally steps run even if one fails."""
        record: list[str] = []

        class TestWorkflow(Workflow):
            def define_steps(self):
                self.add_step(PassingStep("main", record))
                self.add_finally(FailingStep("cleanup1", record))
                self.add_finally(PassingStep("cleanup2", record))
                self.add_finally(ExceptionStep("cleanup3", record))
                self.add_finally(PassingStep("cleanup4", record))

        workflow = TestWorkflow(context)
        result = workflow.execute()

        # Main workflow succeeded, finally failures don't affect overall success
        assert result.success
        assert record == [
            "executed:main",
            "executed:cleanup1",
            "executed:cleanup2",
            "executed:cleanup3",
            "executed:cleanup4",
        ]

    def test_empty_workflow(self, context):
        """Empty workflow completes successfully."""

        class TestWorkflow(Workflow):
            def define_steps(self):
                pass

        workflow = TestWorkflow(context)
        result = workflow.execute()

        assert result.success
        assert len(result.step_results) == 0

    def test_duration_tracking(self, context):
        """Workflow tracks total duration."""
        record: list[str] = []

        class TestWorkflow(Workflow):
            def define_steps(self):
                self.add_step(PassingStep("step1", record))

        workflow = TestWorkflow(context)
        result = workflow.execute()

        assert result.duration_seconds >= 0
        assert result.start_time is not None
        assert result.end_time is not None
        assert result.run_uuid == context.run_uuid


class TestStepResult:
    """Tests for StepResult helper methods."""

    def test_ok_result(self):
        """StepResult.ok creates successful result."""
        result = StepResult.ok("All good", foo="bar")

        assert result.success
        assert result.message == "All good"
        assert result.data == {"foo": "bar"}
        assert result.error is None

    def test_fail_result(self):
        """StepResult.fail creates failed result."""
        error = ValueError("bad input")
        result = StepResult.fail("Something went wrong", error=error)

        assert not result.success
        assert result.message == "Something went wrong"
        assert result.error is error
