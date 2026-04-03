"""Unit tests for artifact collection steps."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from projects.core.steps import CleanupDeploymentStep, CollectArtifactsStep
from projects.core.workflow import WorkflowContext


class TestCollectArtifactsStep:
    """Tests for CollectArtifactsStep."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("collect_artifacts")
        return ctx

    def test_step_name(self):
        """Step has correct default name."""
        step = CollectArtifactsStep()
        assert step.name == "collect_artifacts"

    def test_custom_app_label(self):
        """Step accepts custom app label."""
        step = CollectArtifactsStep(app_label="custom-app")
        assert step.app_label == "custom-app"

    @patch("subprocess.run")
    def test_collects_logs(self, mock_run, context):
        """CollectArtifactsStep collects pod logs."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="log output", stderr=""
        )

        step = CollectArtifactsStep(app_label="test-app")
        result = step.execute(context)

        assert result.success
        # Verify oc logs was called
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("logs" in str(c) for c in calls)

    @patch("subprocess.run")
    def test_never_fails(self, mock_run, context):
        """CollectArtifactsStep never fails the workflow."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="command failed"
        )

        step = CollectArtifactsStep()
        result = step.execute(context)

        # Should succeed even if oc commands fail
        assert result.success


class TestCleanupDeploymentStep:
    """Tests for CleanupDeploymentStep."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("cleanup")
        return ctx

    def test_step_name(self):
        """Step has correct default name."""
        step = CleanupDeploymentStep(deployment_name="test")
        assert step.name == "cleanup"

    @patch("subprocess.run")
    def test_deletes_deployment(self, mock_run, context):
        """CleanupDeploymentStep deletes deployment."""
        mock_run.return_value = MagicMock(returncode=0)

        step = CleanupDeploymentStep(deployment_name="test-deploy")
        result = step.execute(context)

        assert result.success
        # Check deployment was deleted
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("deployment" in str(c) for c in calls)

    @patch("subprocess.run")
    def test_deletes_service_and_route(self, mock_run, context):
        """CleanupDeploymentStep deletes associated resources."""
        mock_run.return_value = MagicMock(returncode=0)

        step = CleanupDeploymentStep(
            deployment_name="test",
            delete_service=True,
            delete_route=True,
        )
        result = step.execute(context)

        assert result.success

    @patch("subprocess.run")
    def test_never_fails(self, mock_run, context):
        """CleanupDeploymentStep never fails the workflow."""
        mock_run.return_value = MagicMock(returncode=1)

        step = CleanupDeploymentStep(deployment_name="test")
        result = step.execute(context)

        # Should succeed even if deletes fail
        assert result.success
