"""Tests for converting a postprocess status YAML to an HTML report.

Covers the distinction between "the test failed" and "the post-processing
failed" in the Final Status card styling.
"""

from pathlib import Path

import yaml

from projects.core.library.status_to_html import convert_status_yaml_to_html


def _write_status(tmp_path: Path, status_data: dict) -> Path:
    """Write a postprocess_status.yaml and return its path."""
    yaml_file = tmp_path / "postprocess_status.yaml"
    yaml_file.write_text(yaml.dump(status_data))
    return yaml_file


def _status(final_status: str, success, phase: str, steps: list | None = None) -> dict:
    """Build a postprocess status dict like CaliperPostProcessor._build_result."""
    return {
        "final_status": final_status,
        "success": success,
        "base_directory": str(Path.cwd()),
        "test_phase": {"phase": phase},
        "steps": steps if steps is not None else [{"parse": {"status": "success"}}],
    }


def _generated_html(tmp_path: Path, status_data: dict) -> str:
    yaml_file = _write_status(tmp_path, status_data)
    html_path = convert_status_yaml_to_html(yaml_file)
    return Path(html_path).read_text()


def test_success_status_card(tmp_path):
    html = _generated_html(tmp_path, _status("success", True, "SUCCESS"))

    assert "status-card success" in html
    assert "status-badge status-passed" in html


def test_test_failed_clean_postprocess_shows_warning_card(tmp_path):
    """A failed test with clean post-processing: amber card, badge still reads test_failed."""
    html = _generated_html(tmp_path, _status("test_failed", False, "FAILED"))

    assert "status-card warning" in html
    assert "status-badge status-warning" in html
    # The badge text keeps the raw final_status — no information lost
    assert ">test_failed</span>" in html
    # The adjacent Test Phase card carries the failure
    assert "<strong>FAILED</strong>" in html


def test_postprocess_failure_shows_failed_card(tmp_path):
    html = _generated_html(
        tmp_path,
        _status(
            "parse_visualize_failed",
            False,
            "SUCCESS",
            steps=[{"parse": {"status": "failed"}}],
        ),
    )

    assert "status-card failed" in html
    assert "status-badge status-failed" in html


def test_masked_step_failure_shows_failed_card(tmp_path):
    """A failed step masked by the test_failed label still shows the failed card."""
    html = _generated_html(
        tmp_path,
        _status(
            "test_failed",
            False,
            "FAILED",
            steps=[{"parse": {"status": "failed"}}],
        ),
    )

    assert "status-card failed" in html


def test_regression_shows_failed_card(tmp_path):
    html = _generated_html(tmp_path, _status("performance_regression", False, "SUCCESS"))

    assert "status-card failed" in html


def test_warning_shows_warning_card(tmp_path):
    html = _generated_html(
        tmp_path,
        _status(
            "kpi_pipeline_failed", "warning", "SUCCESS", steps=[{"kpi": {"status": "warning"}}]
        ),
    )

    assert "status-card warning" in html
    assert "status-badge status-warning" in html
