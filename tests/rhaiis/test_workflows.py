"""Unit tests for RHAIIS workflows."""

import tempfile
from pathlib import Path

import pytest

from projects.core.workflow import WorkflowContext
from projects.rhaiis.workflows import BenchmarkWorkflow, CleanupWorkflow, PrepareWorkflow


class TestBenchmarkWorkflow:
    """Tests for BenchmarkWorkflow."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        return WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

    def test_workflow_defines_steps(self, context):
        """BenchmarkWorkflow defines required steps."""
        workflow = BenchmarkWorkflow(
            ctx=context,
            model="Qwen/Qwen3-0.6B",
            workload="balanced",
        )

        # Force step definition
        workflow._ensure_defined()

        # Should have deploy, wait, benchmark steps
        step_names = [s.name for s in workflow.steps]
        assert "deploy" in step_names
        assert "wait" in step_names
        assert "benchmark" in step_names

        # Should have finally steps
        finally_names = [s.name for s in workflow.finally_steps]
        assert "collect_artifacts" in finally_names
        assert "cleanup" in finally_names

    def test_workflow_uses_custom_image(self, context):
        """BenchmarkWorkflow uses custom vLLM image."""
        custom_image = "custom/vllm:latest"
        workflow = BenchmarkWorkflow(
            ctx=context,
            model="test/model",
            vllm_image=custom_image,
        )

        assert workflow.vllm_image == custom_image

    def test_workflow_sanitizes_deployment_name(self, context):
        """BenchmarkWorkflow sanitizes deployment name."""
        workflow = BenchmarkWorkflow(
            ctx=context,
            model="Qwen/Qwen3-0.6B-Instruct",
        )

        # Should be lowercase, no special chars
        assert workflow.deployment_name == "qwen3-0-6b-instruct"

    def test_workflow_uses_env_image(self, temp_artifact_dir, monkeypatch):
        """BenchmarkWorkflow uses FORGE_VLLM_IMAGE from env."""
        monkeypatch.setenv("FORGE_VLLM_IMAGE", "env/vllm:test")
        context = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        workflow = BenchmarkWorkflow(ctx=context, model="test/model")

        assert workflow.vllm_image == "env/vllm:test"


class TestPrepareWorkflow:
    """Tests for PrepareWorkflow."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        return WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

    def test_prepare_defines_operator_steps(self, context):
        """PrepareWorkflow defines operator installation steps."""
        workflow = PrepareWorkflow(ctx=context, rhoai_version="2.19")
        workflow._ensure_defined()

        step_names = [s.name for s in workflow.steps]
        assert "install_nfd" in step_names
        assert "install_gpu" in step_names
        assert "install_rhoai" in step_names


class TestCleanupWorkflow:
    """Tests for CleanupWorkflow."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        return WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

    def test_cleanup_defines_steps(self, context):
        """CleanupWorkflow defines cleanup steps."""
        workflow = CleanupWorkflow(ctx=context, namespace="test-ns")
        workflow._ensure_defined()

        step_names = [s.name for s in workflow.steps]
        assert "cleanup_namespace" in step_names
