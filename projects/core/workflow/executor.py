"""Workflow executors."""

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .step import StepResult
from .workflow import WorkflowResult

if TYPE_CHECKING:
    from .workflow import Workflow

logger = logging.getLogger(__name__)


class SequentialExecutor:
    """
    Execute workflow steps sequentially with finally block support.

    Execution flow:
    1. Run normal steps in order until completion or failure
    2. On failure, skip remaining normal steps
    3. Always run finally steps, even if normal steps failed
    4. Collect all results and return WorkflowResult
    """

    def execute(self, workflow: "Workflow") -> WorkflowResult:
        """
        Execute the workflow.

        Args:
            workflow: Workflow instance to execute

        Returns:
            WorkflowResult with all step outcomes
        """
        start_time = datetime.now(timezone.utc)
        step_results: dict[str, StepResult] = {}
        failed_step: str | None = None
        original_error: Exception | None = None

        ctx = workflow.ctx
        logger.info(f"Starting workflow run {ctx.run_uuid}")

        # Run normal steps
        for step in workflow.steps:
            step_name = step.name
            logger.info(f"Running step: {step_name}")

            # Get artifact directory for this step
            step_artifact_dir = ctx.get_step_artifact_dir(step_name)

            step_start = time.monotonic()
            try:
                result = step.execute(ctx)
                result.duration_seconds = time.monotonic() - step_start
                result.start_time = datetime.now(timezone.utc)
                step_results[step_name] = result

                if not result.success:
                    logger.error(f"Step {step_name} failed: {result.message}")
                    failed_step = step_name
                    original_error = result.error
                    break
                logger.info(f"Step {step_name} completed in {result.duration_seconds:.2f}s")

            except Exception as e:
                duration = time.monotonic() - step_start
                logger.exception(f"Step {step_name} raised exception")
                step_results[step_name] = StepResult(
                    success=False,
                    message=f"Exception: {e}",
                    error=e,
                    duration_seconds=duration,
                )
                failed_step = step_name
                original_error = e
                break

        # Run finally steps (always)
        finally_errors: list[Exception] = []
        for step in workflow.finally_steps:
            step_name = step.name
            logger.info(f"Running finally step: {step_name}")

            step_artifact_dir = ctx.get_step_artifact_dir(step_name)

            step_start = time.monotonic()
            try:
                result = step.execute(ctx)
                result.duration_seconds = time.monotonic() - step_start
                step_results[step_name] = result

                if not result.success:
                    logger.warning(f"Finally step {step_name} failed: {result.message}")
                    # Don't break - continue with other finally steps
                    if result.error:
                        finally_errors.append(result.error)
                else:
                    logger.info(f"Finally step {step_name} completed in {result.duration_seconds:.2f}s")

            except Exception as e:
                duration = time.monotonic() - step_start
                logger.exception(f"Finally step {step_name} raised exception")
                step_results[step_name] = StepResult(
                    success=False,
                    message=f"Exception: {e}",
                    error=e,
                    duration_seconds=duration,
                )
                finally_errors.append(e)
                # Continue with other finally steps

        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()

        workflow_success = failed_step is None
        logger.info(
            f"Workflow completed: success={workflow_success}, "
            f"duration={duration:.2f}s, failed_step={failed_step}"
        )

        return WorkflowResult(
            success=workflow_success,
            step_results=step_results,
            failed_step=failed_step,
            duration_seconds=duration,
            run_uuid=ctx.run_uuid,
            start_time=start_time,
            end_time=end_time,
        )
