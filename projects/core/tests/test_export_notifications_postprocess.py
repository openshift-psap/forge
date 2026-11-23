"""Tests for the postprocess-status consumers in export_notifications.

Covers the distinction between "the test failed" and "the post-processing
failed": a failed test must not report the post-processing as failed, and a
real post-processing failure must raise the FAILURE flag.
"""

from pathlib import Path

import yaml

from projects.core.library.export_notifications import (
    _check_postprocess_warnings,
    _extract_postprocess_status_info,
)
from projects.core.library.step_status import StepStatus


def _write_postprocess_status(step_dir: Path, status_data: dict) -> Path:
    """Write a postprocess_status.yaml at the step root and return its path."""
    status_file = step_dir / "postprocess_status.yaml"
    status_file.write_text(yaml.dump(status_data))
    return status_file


def _test_failed_clean_steps() -> dict:
    """Test failed, all post-processing steps succeeded."""
    return {
        "final_status": "test_failed",
        "success": False,
        "base_directory": "/test/artifacts",
        "test_phase": {"phase": "FAILED", "message": "exit code 1"},
        "steps": [
            {"parse": {"status": "success", "completed_at": 1.0}},
            {"visualize": {"status": "success", "completed_at": 2.0}},
        ],
    }


def _postprocess_failure() -> dict:
    """Test passed, a post-processing step failed."""
    return {
        "final_status": "parse_visualize_failed",
        "success": False,
        "base_directory": "/test/artifacts",
        "test_phase": {"phase": "SUCCESS"},
        "steps": [{"parse": {"status": "failed", "completed_at": 1.0}}],
    }


def _postprocess_warning() -> dict:
    """Test passed, post-processing completed with warnings only."""
    return {
        "final_status": "kpi_pipeline_failed",
        "success": "warning",
        "base_directory": "/test/artifacts",
        "test_phase": {"phase": "SUCCESS"},
        "steps": [{"artifacts_to_kpis": {"status": "warning", "completed_at": 1.0}}],
    }


def test_extract_postprocess_status_info_test_failed_clean(tmp_path):
    """A failed test with clean post-processing shows a healthy post-processing line."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    _write_postprocess_status(step_dir, _test_failed_clean_steps())

    lines = _extract_postprocess_status_info(tmp_path)

    assert len(lines) == 1
    assert "⚠️" in lines[0]
    assert "❌" not in lines[0]
    assert "⚠️ test: failed" in lines[0]
    assert "✔️ parse" in lines[0]


def test_extract_postprocess_status_info_postprocess_failure(tmp_path):
    """A post-processing step failure shows the failure emoji."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    _write_postprocess_status(step_dir, _postprocess_failure())

    lines = _extract_postprocess_status_info(tmp_path)

    assert len(lines) == 1
    assert "❌" in lines[0]
    assert "⚠️ test: failed" not in lines[0]


def test_extract_postprocess_status_info_warning(tmp_path):
    """A warnings-only post-processing shows the warning emoji."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    _write_postprocess_status(step_dir, _postprocess_warning())

    lines = _extract_postprocess_status_info(tmp_path)

    assert len(lines) == 1
    assert "⚠️" in lines[0]


def test_extract_postprocess_status_info_no_file(tmp_path):
    """No postprocess status file means no lines."""
    assert _extract_postprocess_status_info(tmp_path) == []


def test_extract_postprocess_status_info_unreadable_file(tmp_path):
    """An unreadable status file produces an error line instead of raising."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    (step_dir / "postprocess_status.yaml").write_text("{{not yaml")

    lines = _extract_postprocess_status_info(tmp_path)

    assert len(lines) == 1
    assert "Error reading file" in lines[0]


def test_check_postprocess_warnings_test_failed_clean(tmp_path):
    """A failed test with clean post-processing raises no flag (test failure
    is already reported via the step's own exit status)."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    _write_postprocess_status(step_dir, _test_failed_clean_steps())

    assert _check_postprocess_warnings(step_dir) == StepStatus.SUCCESS


def test_check_postprocess_warnings_postprocess_failure(tmp_path):
    """A real post-processing failure raises the FAILURE flag."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    _write_postprocess_status(step_dir, _postprocess_failure())

    assert _check_postprocess_warnings(step_dir) == StepStatus.FAILURE


def test_check_postprocess_warnings_masked_step_failure(tmp_path):
    """A failed step masked by the test_failed label still raises the FAILURE flag."""
    status_data = _test_failed_clean_steps()
    status_data["steps"] = [{"parse": {"status": "failed", "completed_at": 1.0}}]

    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    _write_postprocess_status(step_dir, status_data)

    assert _check_postprocess_warnings(step_dir) == StepStatus.FAILURE


def test_check_postprocess_warnings_warning(tmp_path):
    """A warnings-only post-processing raises the WARNING flag."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    _write_postprocess_status(step_dir, _postprocess_warning())

    assert _check_postprocess_warnings(step_dir) == StepStatus.WARNING


def test_check_postprocess_warnings_unreadable_yaml(tmp_path):
    """An unreadable status file raises the WARNING flag, not an exception."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()
    (step_dir / "postprocess_status.yaml").write_text("{not yaml")

    assert _check_postprocess_warnings(step_dir) == StepStatus.WARNING


def test_check_postprocess_warnings_no_file(tmp_path):
    """No postprocess status file means no flag."""
    step_dir = tmp_path / "000__test"
    step_dir.mkdir()

    assert _check_postprocess_warnings(step_dir) == StepStatus.SUCCESS
