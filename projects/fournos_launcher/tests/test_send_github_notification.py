"""Tests for the fournos_launcher completion notification message."""

from pathlib import Path

from projects.core.library import ci as ci_lib
from projects.fournos_launcher.orchestration.submit import (
    _format_operational_notifications,
    _notification_warning_exit_status,
)


def _make_notifications_dir(base: Path) -> Path:
    notifications_dir = base / "000__ci_metadata" / "notifications"
    notifications_dir.mkdir(parents=True)
    return notifications_dir


def test_no_notifications_dir_returns_empty(tmp_path):
    assert _format_operational_notifications(tmp_path / "000__ci_metadata") == ""


def test_empty_notifications_dir_returns_empty(tmp_path):
    _make_notifications_dir(tmp_path)

    assert _format_operational_notifications(tmp_path / "000__ci_metadata") == ""


def test_notification_files_are_included(tmp_path):
    notifications_dir = _make_notifications_dir(tmp_path)
    (notifications_dir / "000__vault_deprecated_filename_vault.pem.txt").write_text(
        "Deprecated vault filename in use: vault/old-name.pem\n\nRename it."
    )
    (notifications_dir / "001__other.txt").write_text("Something else happened.")

    section = _format_operational_notifications(tmp_path / "000__ci_metadata")

    assert "**Operational notifications:**" in section

    # files are rendered in sorted order, with a titled header and quoted content
    assert "##### Vault Deprecated Filename Vault.Pem" in section
    assert "> Deprecated vault filename in use: vault/old-name.pem" in section
    assert "> Rename it." in section
    assert "##### Other" in section
    assert "> Something else happened." in section

    # the deprecated-file notice comes first (sorted by filename)
    assert section.index("Vault Deprecated Filename") < section.index("##### Other")


def test_empty_notification_file_is_skipped(tmp_path):
    notifications_dir = _make_notifications_dir(tmp_path)
    (notifications_dir / "000__empty.txt").write_text("   \n")

    assert _format_operational_notifications(tmp_path / "000__ci_metadata") == ""


def test_notification_warning_exit_status_shape():
    exit_code, category, reason = _notification_warning_exit_status()

    assert exit_code == 0
    assert category == ci_lib.ExitCategory.WARNING
    assert "notification" in reason
