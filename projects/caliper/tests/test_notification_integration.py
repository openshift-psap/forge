"""
Test integration between typed postprocess status models and notification formatting.
"""

import sys
import time
from pathlib import Path

# Add the project root to sys.path to import modules directly
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

# Import notification function directly
import importlib.util

from projects.caliper.public import (
    FinalPostprocessStatus,
    PostprocessStatus,
    PostprocessTestPhase,
    PostprocessTestPhaseInfo,
)

notification_spec = importlib.util.spec_from_file_location(
    "notification", Path(__file__).parent.parent / "orchestration" / "notification.py"
)
notification_module = importlib.util.module_from_spec(notification_spec)
notification_spec.loader.exec_module(notification_module)

format_postprocess_status_notification = notification_module.format_postprocess_status_notification


def test_notification_formatting_success():
    """Test notification formatting for successful postprocess."""
    status = PostprocessStatus(
        final_status=FinalPostprocessStatus.SUCCESS,
        success=True,
        base_directory="/test/artifacts",
        test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS),
        steps=[
            {
                "parse": {
                    "status": "success",
                    "completed_at": time.time(),
                    "record_count": 42,
                    "log_file": "logs/001_parse.log",
                }
            },
            {
                "visualize": {
                    "status": "success",
                    "completed_at": time.time(),
                    "output_dir": "plots/",
                    "output_files": ["plot1.png", "plot2.png"],
                    "log_file": "logs/002_visualize.log",
                }
            },
        ],
    )

    # Mock file link function
    def mock_get_file_link(file_path: str) -> str:
        return f"https://example.com/artifacts/{file_path}"

    notification_text = format_postprocess_status_notification(status, mock_get_file_link)
    print(f"Generated notification text:\n{notification_text}\n")

    # Verify overall structure
    assert "**Post-processing Status** ✅" in notification_text
    assert (
        "- ✅ [**parse**](https://example.com/artifacts/logs/001_parse.log): `success`"
        in notification_text
    )
    assert (
        "- ✅ [**visualize**](https://example.com/artifacts/logs/002_visualize.log): `success`"
        in notification_text
    )

    # Verify file links are included
    assert "📊 [plot1.png](https://example.com/artifacts/plots/plot1.png)" in notification_text
    assert "📊 [plot2.png](https://example.com/artifacts/plots/plot2.png)" in notification_text

    print("✅ Success notification formatting test passed")


def test_notification_formatting_regression():
    """Test notification formatting for regression detection."""
    status = PostprocessStatus(
        final_status=FinalPostprocessStatus.PERFORMANCE_REGRESSION,
        success=False,
        base_directory="/test/artifacts",
        test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS),
        steps=[
            {
                "parse": {
                    "status": "success",
                    "completed_at": time.time(),
                    "record_count": 100,
                }
            },
            {
                "analyse_kpis": {
                    "status": "regression_detected",
                    "completed_at": time.time(),
                    "output_file": "analysis.yaml",
                    "regression_count": 3,
                    "total_kpis": 10,
                    "baseline_files_count": 5,
                    "regressions_detected": True,
                }
            },
        ],
    )

    def mock_get_file_link(file_path: str) -> str:
        return f"https://example.com/artifacts/{file_path}"

    notification_text = format_postprocess_status_notification(status, mock_get_file_link)

    # Verify overall failure status
    assert "**Post-processing Status** ❌" in notification_text

    # Verify parse success
    assert "- ✅ **parse**: `success`" in notification_text

    # Verify regression detection
    assert "- 🚨 **analyse_kpis**: `regression_detected`" in notification_text

    # Verify analysis output file link
    assert "📊 [analysis.yaml](https://example.com/artifacts/analysis.yaml)" in notification_text

    # Verify baseline files count
    assert "📈 Baseline files analyzed: `5`" in notification_text

    print("✅ Regression notification formatting test passed")


def test_notification_formatting_failure():
    """Test notification formatting for failed steps."""
    status = PostprocessStatus(
        final_status=FinalPostprocessStatus.PARSE_VISUALIZE_FAILED,
        success=False,
        base_directory="/test/artifacts",
        test_phase=PostprocessTestPhaseInfo(
            phase=PostprocessTestPhase.FAILED, message="Test execution failed"
        ),
        steps=[
            {
                "parse": {
                    "status": "failed",
                    "completed_at": time.time(),
                    "error": "Invalid JSON format in input file",
                    "reason": "Parsing failed due to malformed data",
                }
            }
        ],
    )

    notification_text = format_postprocess_status_notification(status)

    # Verify overall failure status
    assert "**Post-processing Status** ❌" in notification_text

    # Verify failed step
    assert "- ❌ **parse**: `failed`" in notification_text
    assert "* `Parsing failed due to malformed data`" in notification_text

    print("✅ Failure notification formatting test passed")


def test_notification_without_file_links():
    """Test notification formatting without file link callback."""
    status = PostprocessStatus(
        final_status=FinalPostprocessStatus.SUCCESS,
        success=True,
        base_directory="/test/artifacts",
        test_phase=PostprocessTestPhaseInfo(phase=PostprocessTestPhase.SUCCESS),
        steps=[
            {
                "parse": {
                    "status": "success",
                    "completed_at": time.time(),
                    "log_file": "logs/parse.log",
                }
            }
        ],
    )

    notification_text = format_postprocess_status_notification(status)

    # Verify basic formatting without links
    assert "**Post-processing Status** ✅" in notification_text
    assert "- ✅ **parse**: `success`" in notification_text
    # Should not contain any links
    assert "https://" not in notification_text

    print("✅ No file links notification formatting test passed")


if __name__ == "__main__":
    # Run tests
    test_notification_formatting_success()
    test_notification_formatting_regression()
    test_notification_formatting_failure()
    test_notification_without_file_links()
    print("✅ All notification integration tests passed!")
