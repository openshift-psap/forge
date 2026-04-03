"""Unit tests for GuideLLM step."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from projects.core.steps import RunGuideLLMStep
from projects.core.workflow import WorkflowContext


class TestRunGuideLLMStep:
    """Tests for RunGuideLLMStep."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("benchmark")
        return ctx

    def test_step_name(self):
        """Step has correct default name."""
        step = RunGuideLLMStep(
            endpoint="http://localhost:8080/v1",
            model="test-model",
        )
        assert step.name == "benchmark"

    def test_custom_step_name(self):
        """Step accepts custom name."""
        step = RunGuideLLMStep(
            endpoint="http://localhost:8080/v1",
            model="test-model",
            name="custom-benchmark",
        )
        assert step.name == "custom-benchmark"

    @patch("subprocess.run")
    def test_execute_success(self, mock_run, context):
        """RunGuideLLMStep executes successfully."""
        # Create mock output file
        step_dir = context.artifact_dir / f"{context.step_number:03d}__{context.current_step_name}"
        step_dir.mkdir(parents=True, exist_ok=True)
        output_file = step_dir / "guidellm_results.json"
        output_file.write_text('{"results": []}')

        # Mock responses for: oc apply, get phase, get logs (with marker), get logs (collect), delete
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="pod created", stderr=""),  # oc apply
            MagicMock(returncode=0, stdout="Running", stderr=""),  # get phase
            MagicMock(returncode=0, stdout="BENCHMARK_COMPLETE", stderr=""),  # get logs (marker check)
            MagicMock(returncode=0, stdout="benchmark logs", stderr=""),  # collect logs
            MagicMock(returncode=0, stdout="pod deleted", stderr=""),  # delete pod
        ]

        step = RunGuideLLMStep(
            endpoint="http://localhost:8080/v1",
            model="test-model",
            workload="balanced",
            max_requests=10,
        )
        result = step.execute(context)

        assert result.success
        assert mock_run.called

    @patch("subprocess.run")
    def test_execute_failure(self, mock_run, context):
        """RunGuideLLMStep handles failure."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="benchmark failed"
        )

        step = RunGuideLLMStep(
            endpoint="http://localhost:8080/v1",
            model="test-model",
        )
        result = step.execute(context)

        assert not result.success
        assert "failed" in result.message.lower()

    @patch("subprocess.run")
    def test_handles_timeout(self, mock_run, context):
        """RunGuideLLMStep handles timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired("guidellm", 60)

        step = RunGuideLLMStep(
            endpoint="http://localhost:8080/v1",
            model="test-model",
            max_seconds=60,
        )
        result = step.execute(context)

        assert not result.success
        assert "timed out" in result.message.lower()

    @patch("subprocess.run")
    def test_handles_missing_command(self, mock_run, context):
        """RunGuideLLMStep handles missing guidellm command."""
        mock_run.side_effect = FileNotFoundError("guidellm not found")

        step = RunGuideLLMStep(
            endpoint="http://localhost:8080/v1",
            model="test-model",
        )
        result = step.execute(context)

        assert not result.success
        assert "not found" in result.message.lower()
