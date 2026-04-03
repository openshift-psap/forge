"""Unit tests for RHAIIS workflow steps."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from projects.core.workflow import WorkflowContext
from projects.rhaiis.workflows.steps import (
    CleanupNamespaceStep,
    DeployVLLMStep,
    WaitForReadyStep,
)


class TestDeployVLLMStep:
    """Tests for DeployVLLMStep (KServe-based)."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        # Pre-increment step number to simulate workflow execution
        ctx.get_step_artifact_dir("deploy")
        return ctx

    @pytest.fixture
    def default_runtime_args(self):
        """Default runtime args for tests."""
        return {
            "gpu-memory-utilization": 0.9,
            "max-model-len": 4096,
            "tensor-parallel-size": 1,
        }

    def test_generates_kserve_yaml(self, context, default_runtime_args):
        """DeployVLLMStep generates valid KServe YAML."""
        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args={**default_runtime_args, "tensor-parallel-size": 2},
            tensor_parallel=2,
            namespace="test-ns",
        )

        yaml_content = step._generate_kserve_yaml()

        assert "apiVersion: serving.kserve.io/v1alpha1" in yaml_content
        assert "kind: ServingRuntime" in yaml_content
        assert "apiVersion: serving.kserve.io/v1beta1" in yaml_content
        assert "kind: InferenceService" in yaml_content
        assert "name: test-deploy" in yaml_content
        assert "namespace: test-ns" in yaml_content
        assert 'nvidia.com/gpu: "2"' in yaml_content

    def test_generates_shared_memory_for_tp(self, context, default_runtime_args):
        """DeployVLLMStep includes shared memory volume for tensor parallel > 1."""
        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args={**default_runtime_args, "tensor-parallel-size": 4},
            tensor_parallel=4,
        )

        yaml_content = step._generate_kserve_yaml()

        assert "shared-memory" in yaml_content
        assert "/dev/shm" in yaml_content
        assert "sizeLimit: 8Gi" in yaml_content

    def test_shared_memory_always_present(self, context, default_runtime_args):
        """DeployVLLMStep includes shared memory even for tensor parallel = 1."""
        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args=default_runtime_args,
            tensor_parallel=1,
        )

        yaml_content = step._generate_kserve_yaml()

        # Shared memory is always required for vLLM
        assert "shared-memory" in yaml_content

    def test_amd_accelerator(self, context, default_runtime_args):
        """DeployVLLMStep uses AMD GPU resources."""
        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args=default_runtime_args,
            accelerator="amd",
            tensor_parallel=1,
        )

        yaml_content = step._generate_kserve_yaml()

        assert "amd.com/gpu" in yaml_content

    def test_hf_storage_source(self, context, default_runtime_args):
        """DeployVLLMStep configures HuggingFace storage."""
        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args=default_runtime_args,
            storage_source="hf",
            storage_path="models-pvc",
        )

        yaml_content = step._generate_kserve_yaml()

        assert "HF_TOKEN" in yaml_content
        assert "HF_HOME" in yaml_content
        assert "pvc://models-pvc" in yaml_content

    def test_custom_runtime_args(self, context):
        """DeployVLLMStep includes custom runtime args."""
        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args={"enable-prefix-caching": True, "max-num-seqs": 256},
        )

        yaml_content = step._generate_kserve_yaml()

        assert "--enable-prefix-caching" in yaml_content
        assert "--max-num-seqs=256" in yaml_content

    @patch("subprocess.run")
    def test_execute_success(self, mock_run, context, default_runtime_args):
        """DeployVLLMStep executes successfully."""
        mock_run.return_value = MagicMock(returncode=0, stdout="created", stderr="")

        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args=default_runtime_args,
        )
        result = step.execute(context)

        assert result.success
        assert mock_run.called

    @patch("subprocess.run")
    def test_execute_failure(self, mock_run, context, default_runtime_args):
        """DeployVLLMStep handles apply failure."""
        # First two calls (namespace creation) succeed, third (apply) fails
        mock_run.side_effect = [
            MagicMock(returncode=0),  # namespace dry-run
            MagicMock(returncode=0),  # namespace apply
            MagicMock(returncode=1, stdout="", stderr="error applying"),  # kserve apply
        ]

        step = DeployVLLMStep(
            model="test/model",
            deployment_name="test-deploy",
            vllm_image="test/image:v1",
            runtime_args=default_runtime_args,
        )
        result = step.execute(context)

        assert not result.success
        assert "error applying" in result.message


class TestWaitForReadyStep:
    """Tests for WaitForReadyStep (InferenceService)."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("wait")
        return ctx

    @patch("subprocess.run")
    def test_wait_success_immediate(self, mock_run, context):
        """WaitForReadyStep succeeds when InferenceService is ready."""
        # Order: status check -> URL -> get pod name -> health check
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="True", stderr=""),  # status check
            MagicMock(returncode=0, stdout="http://test.svc", stderr=""),  # URL
            MagicMock(returncode=0, stdout="test-pod-abc", stderr=""),  # get pod name
            MagicMock(returncode=0, stdout="200", stderr=""),  # health check curl
        ]

        step = WaitForReadyStep(
            deployment_name="test",
            timeout_seconds=30,
            poll_interval=1,
        )
        result = step.execute(context)

        assert result.success
        assert "ready" in result.message.lower()
        assert result.data.get("service_url") == "http://test.svc"

    @patch("subprocess.run")
    @patch("time.sleep")
    def test_wait_timeout(self, mock_sleep, mock_run, context):
        """WaitForReadyStep fails on timeout."""
        mock_run.return_value = MagicMock(returncode=0, stdout="False", stderr="")

        step = WaitForReadyStep(
            deployment_name="test",
            timeout_seconds=2,
            poll_interval=1,
        )
        result = step.execute(context)

        assert not result.success
        assert "not ready" in result.message.lower()


class TestCleanupNamespaceStep:
    """Tests for CleanupNamespaceStep."""

    @pytest.fixture
    def temp_artifact_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def context(self, temp_artifact_dir):
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.get_step_artifact_dir("cleanup")
        return ctx

    @patch("subprocess.run")
    def test_cleanup_success(self, mock_run, context):
        """CleanupNamespaceStep cleans up resources."""
        mock_run.return_value = MagicMock(returncode=0)

        step = CleanupNamespaceStep(namespace="test-ns")
        result = step.execute(context)

        # Cleanup step never fails
        assert result.success

    @patch("subprocess.run")
    def test_cleanup_with_errors(self, mock_run, context):
        """CleanupNamespaceStep handles errors gracefully."""
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")

        step = CleanupNamespaceStep(namespace="test-ns")
        result = step.execute(context)

        # Still succeeds - cleanup is best effort
        assert result.success
