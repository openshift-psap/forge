import pytest

from projects.core.notifications import send as notifications


def test_send_notification_skips_github_without_pull_number(monkeypatch):
    monkeypatch.delenv("PULL_NUMBER", raising=False)
    monkeypatch.setattr(
        notifications.vault_lib,
        "get_vault_manager",
        lambda: pytest.fail("non-PR notifications should not load the vault"),
    )

    assert (
        notifications.send_notification(
            "completion message",
            github=True,
            notification_vault="notifications",
        )
        is True
    )
