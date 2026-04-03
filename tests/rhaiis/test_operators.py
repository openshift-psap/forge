"""Unit tests for RHAIIS operator installation steps."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from projects.core.workflow import WorkflowContext
from projects.rhaiis.workflows.steps import (
    InstallGPUOperatorStep,
    InstallNFDOperatorStep,
    InstallRHOAIOperatorStep,
)


class TestInstallNFDOperatorStep:
    """Tests for InstallNFDOperatorStep."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("install_nfd")
        return ctx

    def test_step_name(self):
        """Step has correct default name."""
        step = InstallNFDOperatorStep()
        assert step.name == "install_nfd"

    @patch("subprocess.run")
    def test_execute_success(self, mock_run, context):
        """InstallNFDOperatorStep executes successfully."""
        mock_run.return_value = MagicMock(returncode=0, stdout="created", stderr="")

        step = InstallNFDOperatorStep()
        result = step.execute(context)

        assert result.success
        assert "NFD" in result.message

    @patch("subprocess.run")
    def test_creates_subscription_yaml(self, mock_run, context):
        """InstallNFDOperatorStep creates subscription YAML."""
        mock_run.return_value = MagicMock(returncode=0)

        step = InstallNFDOperatorStep()
        result = step.execute(context)

        # Check YAML file was created
        step_dir = context.artifact_dir / f"{context.step_number:03d}__{context.current_step_name}"
        yaml_file = step_dir / "nfd-subscription.yaml"
        assert yaml_file.exists()
        content = yaml_file.read_text()
        assert "openshift-nfd" in content


class TestInstallGPUOperatorStep:
    """Tests for InstallGPUOperatorStep."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("install_gpu")
        return ctx

    def test_step_name(self):
        """Step has correct default name."""
        step = InstallGPUOperatorStep()
        assert step.name == "install_gpu"

    @patch("subprocess.run")
    def test_execute_success(self, mock_run, context):
        """InstallGPUOperatorStep executes successfully."""
        mock_run.return_value = MagicMock(returncode=0, stdout="created", stderr="")

        step = InstallGPUOperatorStep()
        result = step.execute(context)

        assert result.success
        assert "GPU" in result.message

    @patch("subprocess.run")
    def test_execute_failure(self, mock_run, context):
        """InstallGPUOperatorStep handles failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        step = InstallGPUOperatorStep()
        result = step.execute(context)

        assert not result.success

    @patch("subprocess.run")
    def test_creates_subscription_yaml(self, mock_run, context):
        """InstallGPUOperatorStep creates subscription YAML."""
        mock_run.return_value = MagicMock(returncode=0)

        step = InstallGPUOperatorStep()
        step.execute(context)

        step_dir = context.artifact_dir / f"{context.step_number:03d}__{context.current_step_name}"
        yaml_file = step_dir / "gpu-subscription.yaml"
        assert yaml_file.exists()
        content = yaml_file.read_text()
        assert "gpu-operator-certified" in content


class TestInstallRHOAIOperatorStep:
    """Tests for InstallRHOAIOperatorStep."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("install_rhoai")
        return ctx

    def test_step_name(self):
        """Step has correct default name."""
        step = InstallRHOAIOperatorStep()
        assert step.name == "install_rhoai"

    def test_custom_version(self):
        """Step accepts custom RHOAI version."""
        step = InstallRHOAIOperatorStep(version="2.20")
        assert step.version == "2.20"

    @patch("subprocess.run")
    def test_execute_success(self, mock_run, context):
        """InstallRHOAIOperatorStep executes successfully."""
        mock_run.return_value = MagicMock(returncode=0, stdout="created", stderr="")

        step = InstallRHOAIOperatorStep(version="2.19")
        result = step.execute(context)

        assert result.success
        assert "RHOAI" in result.message
        assert "2.19" in result.message

    @patch("subprocess.run")
    def test_execute_failure(self, mock_run, context):
        """InstallRHOAIOperatorStep handles failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        step = InstallRHOAIOperatorStep()
        result = step.execute(context)

        assert not result.success

    @patch("subprocess.run")
    def test_creates_subscription_yaml_with_channel(self, mock_run, context):
        """InstallRHOAIOperatorStep creates subscription with correct channel."""
        mock_run.return_value = MagicMock(returncode=0)

        step = InstallRHOAIOperatorStep(version="2.19")
        step.execute(context)

        step_dir = context.artifact_dir / f"{context.step_number:03d}__{context.current_step_name}"
        yaml_file = step_dir / "rhoai-subscription.yaml"
        assert yaml_file.exists()
        content = yaml_file.read_text()
        assert "stable-2.19" in content
        assert "rhods-operator" in content
