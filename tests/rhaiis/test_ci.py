"""Unit tests for RHAIIS CI CLI."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from projects.rhaiis.orchestration.ci import ci


class TestCiPrepare:
    """Tests for ci prepare command."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_prepare_dry_run(self, runner):
        """prepare --dry-run shows what would be done."""
        result = runner.invoke(ci, ["prepare", "--dry-run"])

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "RHOAI" in result.output


class TestCiTest:
    """Tests for ci test command."""

    @pytest.fixture
    def runner(self):
        return CliRunner(env={"FORGE_ARTIFACT_DIR": "/tmp/artifacts"})

    def test_test_dry_run(self, runner):
        """test --dry-run shows model from env."""
        result = runner.invoke(
            ci, ["test", "--dry-run"],
            env={"FORGE_MODEL": "test/model", "FORGE_ARTIFACT_DIR": "/tmp/artifacts"}
        )

        assert result.exit_code == 0
        assert "test/model" in result.output

    def test_test_dry_run_with_workloads(self, runner):
        """test --dry-run shows workloads from env."""
        result = runner.invoke(
            ci, ["test", "--dry-run"],
            env={
                "FORGE_MODEL": "test/model",
                "FORGE_WORKLOADS": "balanced,heterogeneous",
                "FORGE_ARTIFACT_DIR": "/tmp/artifacts",
            }
        )

        assert result.exit_code == 0
        assert "balanced" in result.output or "heterogeneous" in result.output

class TestCiCleanup:
    """Tests for ci cleanup command."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_cleanup_dry_run(self, runner):
        """cleanup --dry-run shows what would be done."""
        result = runner.invoke(ci, ["cleanup", "--dry-run"])

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output

    def test_cleanup_with_namespace(self, runner):
        """cleanup accepts custom namespace via env."""
        result = runner.invoke(
            ci, ["cleanup", "--dry-run"],
            env={"FORGE_NAMESPACE": "custom-ns"}
        )

        assert result.exit_code == 0
        assert "custom-ns" in result.output
