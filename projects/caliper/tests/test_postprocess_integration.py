"""
Integration tests for the complete postprocess status model system.

Tests the end-to-end flow from orchestration generation to notification parsing.
"""

import tempfile
import time
from pathlib import Path

from projects.caliper.public import (
    FinalPostprocessStatus,
    PostprocessStatus,
    PostprocessTestPhase,
    PostprocessTestPhaseInfo,
    StepStatus,
    load_postprocess_status_yaml,
    save_postprocess_status_yaml,
)


def test_postprocess_status_roundtrip():
    """Test complete YAML roundtrip for postprocess status."""
    # Create a comprehensive status object
    original_status = PostprocessStatus(
        final_status=FinalPostprocessStatus.PERFORMANCE_REGRESSION,
        success=False,
        base_directory="/test/artifacts",
        test_phase=PostprocessTestPhaseInfo(
            phase=PostprocessTestPhase.SUCCESS, message="All tests passed"
        ),
        steps=[
            {
                "parse": {
                    "status": "success",
                    "completed_at": 1234567890.0,
                    "plugin_module": "test.plugin",
                    "record_count": 42,
                }
            },
            {
                "analyse_kpis": {
                    "status": "regression_detected",
                    "completed_at": 1234567891.0,
                    "output_file": "analysis.yaml",
                    "total_kpis": 10,
                    "regression_count": 3,
                    "regressions_detected": True,
                }
            },
            {
                "visualize": {
                    "status": "success",
                    "completed_at": 1234567892.0,
                    "output_dir": "plots/",
                    "generated_files": 5,
                }
            },
        ],
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml_file = Path(f.name)

    try:
        # Save to YAML
        save_postprocess_status_yaml(original_status, yaml_file)
        assert yaml_file.exists()

        # Load from YAML
        loaded_status = load_postprocess_status_yaml(yaml_file)

        # Verify all fields match
        assert loaded_status.final_status == original_status.final_status
        assert loaded_status.success == original_status.success
        assert loaded_status.base_directory == original_status.base_directory
        assert loaded_status.test_phase.phase == original_status.test_phase.phase
        assert loaded_status.test_phase.message == original_status.test_phase.message
        assert len(loaded_status.steps) == len(original_status.steps)

        # Verify step data
        for original_step, loaded_step in zip(
            original_status.steps, loaded_status.steps, strict=False
        ):
            assert original_step.keys() == loaded_step.keys()
            for step_name, original_data in original_step.items():
                loaded_data = loaded_step[step_name]
                assert loaded_data["status"] == original_data["status"]
                assert loaded_data["completed_at"] == original_data["completed_at"]

    finally:
        yaml_file.unlink()


def test_postprocess_status_api_methods():
    """Test the rich API methods on PostprocessStatus."""
    # Success case
    success_status = PostprocessStatus(
        final_status=FinalPostprocessStatus.SUCCESS,
        success=True,
        base_directory="/test",
        test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS),
        steps=[],
    )

    assert success_status.is_success()
    assert not success_status.has_regressions()
    assert success_status.get_failure_reason() is None

    # Regression case
    regression_status = PostprocessStatus(
        final_status=FinalPostprocessStatus.PERFORMANCE_REGRESSION,
        success=False,
        base_directory="/test",
        test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS),
        steps=[
            {
                "analyse_kpis": {
                    "status": "regression_detected",
                    "regression_count": 5,
                    "total_kpis": 20,
                    "regressions_detected": True,
                    "completed_at": time.time(),
                }
            }
        ],
    )

    assert not regression_status.is_success()
    assert regression_status.has_regressions()
    assert "regression" in regression_status.get_failure_reason().lower()

    # Get specific step
    analysis_result = regression_status.get_step_result("analyse_kpis")
    assert analysis_result is not None
    assert analysis_result["regression_count"] == 5
    assert analysis_result["total_kpis"] == 20

    # Non-existent step
    missing_result = regression_status.get_step_result("nonexistent")
    assert missing_result is None


def test_postprocess_status_from_orchestration_result():
    """Test conversion from orchestration result dict."""
    orchestration_result = {
        "final_status": "performance_regression",
        "success": False,
        "base_directory": "/artifacts",
        "test_phase": {"phase": "SUCCESS", "message": "Tests completed"},
        "steps": [
            {"parse": {"status": "success", "completed_at": 1234567890.0, "record_count": 100}},
            {
                "analyse_kpis": {
                    "status": "regression_detected",
                    "completed_at": 1234567891.0,
                    "regression_count": 2,
                }
            },
        ],
    }

    status = PostprocessStatus.from_orchestration_result(orchestration_result)

    assert status.final_status == FinalPostprocessStatus.PERFORMANCE_REGRESSION
    assert not status.success
    assert status.base_directory == "/artifacts"
    assert status.test_phase.phase == PostprocessTestPhase.SUCCESS
    assert status.test_phase.message == "Tests completed"
    assert len(status.steps) == 2

    # Verify step conversion
    parse_step = status.get_step_result("parse")
    assert parse_step["status"] == "success"
    assert parse_step["record_count"] == 100

    analysis_step = status.get_step_result("analyse_kpis")
    assert analysis_step["status"] == "regression_detected"
    assert analysis_step["regression_count"] == 2


def test_postprocess_status_enum_values():
    """Test that all enum values work correctly."""

    # Test all final status values
    for status_val in FinalPostprocessStatus:
        status = PostprocessStatus(
            final_status=status_val,
            success=status_val == FinalPostprocessStatus.SUCCESS,
            base_directory="/test",
            test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS),
            steps=[],
        )
        assert status.final_status == status_val

    # Test all test phase values
    for phase_val in PostprocessTestPhase:
        test_phase = PostprocessTestPhaseInfo(phase=phase_val)
        assert test_phase.phase == phase_val

    # Test all step status values
    for step_status in StepStatus:
        step_data = {"status": step_status.value, "completed_at": time.time()}
        # Just verify the value can be used
        assert step_data["status"] == step_status.value


def test_notification_pattern_matching():
    """Test pattern matching support for notification systems."""

    def get_notification_emoji(status: PostprocessStatus) -> str:
        """Example notification logic using pattern matching."""
        match status.final_status:
            case FinalPostprocessStatus.SUCCESS:
                return "✅"
            case FinalPostprocessStatus.PERFORMANCE_REGRESSION:
                return "🚨"
            case FinalPostprocessStatus.PERFORMANCE_INCREASE:
                return "📈"
            case FinalPostprocessStatus.TEST_FAILED:
                return "❌"
            case _:
                return "⚠️"

    # Test each case
    test_cases = [
        (FinalPostprocessStatus.SUCCESS, "✅"),
        (FinalPostprocessStatus.PERFORMANCE_REGRESSION, "🚨"),
        (FinalPostprocessStatus.PERFORMANCE_INCREASE, "📈"),
        (FinalPostprocessStatus.TEST_FAILED, "❌"),
        (FinalPostprocessStatus.PARSE_VISUALIZE_FAILED, "⚠️"),
    ]

    for final_status, expected_emoji in test_cases:
        status = PostprocessStatus(
            final_status=final_status,
            success=final_status == FinalPostprocessStatus.SUCCESS,
            base_directory="/test",
            test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS),
            steps=[],
        )
        assert get_notification_emoji(status) == expected_emoji


def test_postprocess_status_yaml_structure():
    """Test that generated YAML has the expected structure."""
    status = PostprocessStatus(
        final_status=FinalPostprocessStatus.SUCCESS,
        success=True,
        base_directory="/test/artifacts",
        test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS, message=None),
        steps=[
            {
                "parse": {
                    "status": "success",
                    "completed_at": 1234567890.0,
                    "plugin_module": "test.plugin",
                    "record_count": 42,
                }
            }
        ],
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml_file = Path(f.name)

    try:
        save_postprocess_status_yaml(status, yaml_file)

        # Read raw YAML content
        with open(yaml_file) as f:
            yaml_content = f.read()

        # Verify expected structure is present
        assert "final_status: success" in yaml_content
        assert "success: true" in yaml_content
        assert "base_directory: /test/artifacts" in yaml_content
        assert "test_phase:" in yaml_content
        assert "phase: SUCCESS" in yaml_content
        assert "steps:" in yaml_content
        assert "parse:" in yaml_content
        assert "status: success" in yaml_content
        assert "record_count: 42" in yaml_content

        # Verify no Python object references
        assert "!!python" not in yaml_content
        assert "object/apply" not in yaml_content

    finally:
        yaml_file.unlink()


def test_write_visualization_var_directive(tmp_path):
    """The /var directive file reproduces the plugin settings and parses as a PR directive."""
    from projects.caliper.orchestration.postprocess import (
        VISUALIZATION_PLUGIN_SETTINGS_KEY,
        VISUALIZATION_VAR_FILENAME,
        _write_visualization_var_directive,
    )
    from projects.core.ci_entrypoint.github.pr_args import handle_var_directive
    from projects.core.library import env

    plugin_settings = {"legend_keys": ["max_requests"], "group_keys": []}

    with env.TempArtifactDir(tmp_path):
        _write_visualization_var_directive(plugin_settings)

    var_line = (tmp_path / VISUALIZATION_VAR_FILENAME).read_text().strip()
    assert var_line == (
        f"/var {VISUALIZATION_PLUGIN_SETTINGS_KEY}: {{group_keys: [], legend_keys: [max_requests]}}"
    )
    # the written line must parse as a valid /var PR directive
    assert handle_var_directive(var_line) == {VISUALIZATION_PLUGIN_SETTINGS_KEY: plugin_settings}


def test_write_visualization_var_directive_no_settings(tmp_path):
    """No plugin settings means no directive file is written."""
    from projects.caliper.orchestration.postprocess import (
        VISUALIZATION_VAR_FILENAME,
        _write_visualization_var_directive,
    )
    from projects.core.library import env

    with env.TempArtifactDir(tmp_path):
        _write_visualization_var_directive(None)
        _write_visualization_var_directive({})

    assert not (tmp_path / VISUALIZATION_VAR_FILENAME).exists()


if __name__ == "__main__":
    # Run basic smoke tests
    test_postprocess_status_roundtrip()
    test_postprocess_status_api_methods()
    test_postprocess_status_from_orchestration_result()
    test_postprocess_status_enum_values()
    test_notification_pattern_matching()
    test_postprocess_status_yaml_structure()
    test_write_visualization_var_directive(Path(tempfile.mkdtemp()))
    test_write_visualization_var_directive_no_settings(Path(tempfile.mkdtemp()))
    print("✅ All postprocess status integration tests passed!")
