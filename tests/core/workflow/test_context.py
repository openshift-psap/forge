"""Unit tests for WorkflowContext."""

import os
import tempfile
from pathlib import Path

import pytest

from projects.core.workflow import WorkflowContext


class TestWorkflowContext:
    """Tests for WorkflowContext."""

    @pytest.fixture
    def temp_artifact_dir(self):
        """Create temporary artifact directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_from_environment_creates_uuid(self, temp_artifact_dir):
        """Context generates a unique run UUID."""
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        assert ctx.run_uuid is not None
        assert len(ctx.run_uuid) == 36  # UUID format

    def test_from_environment_creates_artifact_dir(self, temp_artifact_dir):
        """Context creates artifact directory."""
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        assert ctx.artifact_dir.exists()
        assert ctx.artifact_dir.is_dir()
        assert (ctx.artifact_dir / "_meta").exists()

    def test_from_environment_captures_forge_vars(self, temp_artifact_dir, monkeypatch):
        """Context captures FORGE_* environment variables."""
        monkeypatch.setenv("FORGE_MODEL", "test-model")
        monkeypatch.setenv("FORGE_VLLM_IMAGE", "test-image")
        monkeypatch.setenv("OTHER_VAR", "should-not-capture")

        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        assert ctx.env_vars["FORGE_MODEL"] == "test-model"
        assert ctx.env_vars["FORGE_VLLM_IMAGE"] == "test-image"
        assert "OTHER_VAR" not in ctx.env_vars

    def test_get_env_with_prefix(self, temp_artifact_dir, monkeypatch):
        """get_env works with FORGE_ prefix."""
        monkeypatch.setenv("FORGE_MODEL", "my-model")
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        # With prefix
        assert ctx.get_env("FORGE_MODEL") == "my-model"
        # Without prefix (auto-added)
        assert ctx.get_env("MODEL") == "my-model"

    def test_get_env_default(self, temp_artifact_dir):
        """get_env returns default for missing vars."""
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        assert ctx.get_env("NONEXISTENT") is None
        assert ctx.get_env("NONEXISTENT", "default-value") == "default-value"

    def test_get_step_artifact_dir(self, temp_artifact_dir):
        """get_step_artifact_dir creates numbered directories."""
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        dir1 = ctx.get_step_artifact_dir("deploy")
        dir2 = ctx.get_step_artifact_dir("benchmark")
        dir3 = ctx.get_step_artifact_dir("cleanup")

        assert dir1.name == "001__deploy"
        assert dir2.name == "002__benchmark"
        assert dir3.name == "003__cleanup"

        assert dir1.exists()
        assert dir2.exists()
        assert dir3.exists()

    def test_write_metadata(self, temp_artifact_dir):
        """write_metadata creates YAML file."""
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))
        ctx.config = {"key": "value"}

        path = ctx.write_metadata(args={"model": "test"})

        assert path.exists()
        content = path.read_text()
        assert "run_uuid" in content
        assert "model: test" in content

    def test_write_restart_script(self, temp_artifact_dir):
        """write_restart_script creates executable script."""
        ctx = WorkflowContext.from_environment(artifact_base=str(temp_artifact_dir))

        command = "python run.py --model test"
        path = ctx.write_restart_script(command)

        assert path.exists()
        assert os.access(path, os.X_OK)  # Executable

        content = path.read_text()
        assert "#!/bin/bash" in content
        assert command in content
        assert ctx.run_uuid in content
