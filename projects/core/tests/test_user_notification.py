"""Tests for the DSL user_notification mechanism and its export-notification consumer."""

from pathlib import Path

import projects.core.library.env as env
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.dsl import user_notification

# import the caliper orchestration package first: export_notifications imports from it,
# and caliper.orchestration.export imports export_notifications back (circular import)
import projects.caliper.orchestration  # noqa: F401  isort: skip

from projects.core.library import ci as ci_lib
from projects.core.library.export_notifications import (
    _check_user_notification_warnings,
    _get_overall_step_status,
    _parse_user_notification_level,
    _process_user_notification_files,
)
from projects.core.library.step_status import StepStatus


def _notifications_dir() -> Path:
    return Path(env.ARTIFACT_DIR) / user_notification.DIRECTORY_NAME


def test_warning_writes_indexed_file():
    path = user_notification.warning("maas-api-key-too-long", "the key is too long")

    expected = _notifications_dir() / "000__warning__maas-api-key-too-long.txt"
    assert path == str(expected)
    assert expected.read_text() == "the key is too long"


def test_levels_and_indices_do_not_collide():
    user_notification.info("first", "info message")
    user_notification.warning("second", "warning message")
    user_notification.error("third", "error message")
    user_notification.warning("fourth", "another warning")

    names = sorted(p.name for p in _notifications_dir().glob("*.txt"))
    assert names == [
        "000__info__first.txt",
        "001__warning__second.txt",
        "002__error__third.txt",
        "003__warning__fourth.txt",
    ]


def test_level_parsing():
    assert _parse_user_notification_level("000__warning__some-name.txt") == "warning"
    assert _parse_user_notification_level("012__error__other_name.txt") == "error"
    assert _parse_user_notification_level("000__critical__unknown-level.txt") is None
    assert _parse_user_notification_level("no-level-marker.txt") is None


def test_process_user_notification_files_renders_levels(tmp_path):
    notifications_dir = tmp_path / user_notification.DIRECTORY_NAME
    notifications_dir.mkdir()
    (notifications_dir / "000__warning__maas-api-key-too-long.txt").write_text(
        "line one\nline two\n"
    )
    (notifications_dir / "001__info__token-created.txt").write_text("all good\n")
    (notifications_dir / "002__error__auth-degraded.txt").write_text("broken\n")

    lines = _process_user_notification_files(tmp_path)

    assert "##### ⚠️ Maas-Api-Key-Too-Long" in lines
    assert "> line one" in lines
    assert "> line two" in lines
    assert "##### ℹ️ Token-Created" in lines
    assert "##### ❌ Auth-Degraded" in lines


def test_process_user_notification_files_empty_step(tmp_path):
    assert _process_user_notification_files(tmp_path) == []


def test_warning_flips_step_status(tmp_path):
    step_dir = tmp_path / "01__my-step"
    step_dir.mkdir()
    (step_dir / "run.log").write_text("log content\n")
    (step_dir / CI_METADATA_DIRNAME).mkdir()

    exit_status = ci_lib.ExitStatus()
    exit_status.add(0, ci_lib.ExitCategory.SUCCESS, "all good")
    exit_status.save(step_dir / CI_METADATA_DIRNAME)

    # no user notification: the step stays successful
    assert _get_overall_step_status(tmp_path) == StepStatus.SUCCESS

    notifications_dir = step_dir / user_notification.DIRECTORY_NAME
    notifications_dir.mkdir()
    (notifications_dir / "000__warning__maas-api-key-too-long.txt").write_text("too long\n")

    # a warning flips the step overall status to WARNING, without failing
    assert _get_overall_step_status(tmp_path) == StepStatus.WARNING


def test_check_user_notification_warnings_ignores_other_levels(tmp_path):
    notifications_dir = tmp_path / user_notification.DIRECTORY_NAME
    notifications_dir.mkdir()

    assert _check_user_notification_warnings(tmp_path) == StepStatus.SUCCESS

    (notifications_dir / "000__error__auth-degraded.txt").write_text("broken\n")
    (notifications_dir / "001__info__token-created.txt").write_text("ok\n")
    assert _check_user_notification_warnings(tmp_path) == StepStatus.SUCCESS

    (notifications_dir / "002__warning__cache-ineffective.txt").write_text("cached out\n")
    assert _check_user_notification_warnings(tmp_path) == StepStatus.WARNING
