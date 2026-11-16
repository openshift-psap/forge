"""Tests for the non-fatal WARNING exit status (ci.ExitCategory.WARNING + prepare_ci.postchecks)."""

from pathlib import Path

from projects.core.ci_entrypoint import prepare_ci
from projects.core.library import ci as ci_lib
from projects.core.library import env


def _metadata_dir() -> Path:
    return Path(env.ARTIFACT_DIR) / "000__ci_metadata"


def _save_exit_status(return_code: int, category: ci_lib.ExitCategory, reason: str) -> None:
    exit_status = ci_lib.ExitStatus()
    exit_status.add(return_code, category, reason)
    exit_status.save(_metadata_dir())


def test_postchecks_success_without_exit_status():
    status = prepare_ci.postchecks(
        "fournos_launcher", "ci submit", None, prepare_ci.FinishReason.SUCCESS, ["submit"]
    )

    assert status.startswith("✅")
    assert "⚠️" not in status


def test_postchecks_success_with_success_exit_status():
    _save_exit_status(0, ci_lib.ExitCategory.SUCCESS, "all good")

    status = prepare_ci.postchecks(
        "fournos_launcher", "ci submit", None, prepare_ci.FinishReason.SUCCESS, ["submit"]
    )

    assert status.startswith("✅")
    assert "⚠️" not in status


def test_postchecks_success_with_warning_exit_status():
    _save_exit_status(
        0,
        ci_lib.ExitCategory.WARNING,
        "the GitHub completion notification could not be sent",
    )

    status = prepare_ci.postchecks(
        "fournos_launcher", "ci submit", None, prepare_ci.FinishReason.SUCCESS, ["submit"]
    )

    # the run is still a success ...
    assert status.startswith("✅")
    # ... but the warning recorded in the exit status is surfaced
    assert "⚠️" in status
    assert "the GitHub completion notification could not be sent" in status


def test_postchecks_failure_with_warning_exit_status():
    _save_exit_status(
        0,
        ci_lib.ExitCategory.WARNING,
        "the GitHub completion notification could not be sent",
    )

    status = prepare_ci.postchecks(
        "fournos_launcher", "ci submit", None, prepare_ci.FinishReason.ERROR, ["submit"]
    )

    # the failure status takes precedence over the warning
    assert status.startswith("❌")
    assert "⚠️" in status


def test_warning_exit_status_round_trip():
    _save_exit_status(
        0,
        ci_lib.ExitCategory.WARNING,
        "the GitHub completion notification could not be sent\nreason: token fetch failed",
    )

    loaded = ci_lib.ExitStatus.load(_metadata_dir())

    assert len(loaded.records) == 1
    record = loaded.records[0]
    assert record.return_code == 0
    assert record.category == ci_lib.ExitCategory.WARNING
    assert record.reason == "the GitHub completion notification could not be sent"
    assert record.details == "reason: token fetch failed"
