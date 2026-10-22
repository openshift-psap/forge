"""
Per-project Slack notification provider interface.

Projects that want custom Slack notifications subclass
SlackNotificationProvider and pass it to CIApp.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import projects.core.notifications.slack.api as slack_api
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.library import vault

logger = logging.getLogger(__name__)


@dataclass
class NotificationContext:
    """Context passed to a notification provider when a notification fires."""

    status: dict[str, Any]
    finish_reason: str
    project_name: str
    pr_number: str | None = None
    job_type: str | None = None
    artifact_dir: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared helpers for notification providers
# ---------------------------------------------------------------------------


def format_notification_content(content: str) -> str:
    """Format notification content for Slack: quote lines and convert bold markers."""
    return content.replace("\n", "\n>").replace("**", "*")


def collect_notification_files(
    artifact_dir: Path | None,
) -> tuple[str, list[str], list[str]]:
    """Collect notification files from all 000__ci_metadata/notifications/ dirs.

    Returns (test_description, regular_notifications, failure_reviews).
    """
    if not artifact_dir or not artifact_dir.is_dir():
        return "", [], []

    test_description = ""
    regular = []
    failure_reviews = []

    for nf in sorted(artifact_dir.rglob(f"{CI_METADATA_DIRNAME}/notifications/*.txt")):
        content = nf.read_text().strip()
        if not content:
            continue

        stem = nf.stem
        name = re.sub(r"^\d+__", "", stem)
        formatted = format_notification_content(content[:1500])

        if name == "TEST_DESCRIPTION":
            test_description = f">{formatted}"
        elif name.startswith("FAILURE_REVIEW"):
            failure_reviews.append(f">{formatted}")
        else:
            regular.append(f"• [notif] {name}\n>{formatted}")

    return test_description, regular, failure_reviews


def collect_failure_errors(artifact_dir: Path | None) -> str:
    """Collect error summaries from FAILURE files across pipeline steps."""
    if not artifact_dir or not artifact_dir.is_dir():
        logger.warning(
            f"collect_failure_errors: artifact_dir={artifact_dir!r} is not a valid directory"
        )
        return ""

    logger.info(f"collect_failure_errors: searching for */FAILURE.txt in {artifact_dir}")
    step_dirs = sorted(d for d in artifact_dir.iterdir() if d.is_dir())
    logger.info(
        f"collect_failure_errors: found {len(step_dirs)} subdirectories: "
        + ", ".join(d.name for d in step_dirs)
    )

    errors = []
    for failure_file in sorted(artifact_dir.glob("*/FAILURE.txt")):
        logger.info(f"collect_failure_errors: found {failure_file}")
        content = failure_file.read_text().strip()
        if not content:
            errors.append(f"{failure_file.parent.name}: unknown error")
            continue

        summary = content.split("\n---\n")[0].strip()
        if not summary:
            summary = content.split("\n\n")[0].strip()
            logger.info(
                f"collect_failure_errors: nothing before '---' separator, "
                f"using first paragraph as summary: {summary!r}"
            )
        errors.append(summary)

    if not errors:
        logger.warning(f"collect_failure_errors: no FAILURE.txt files found in {artifact_dir}/*/")

    return "\n".join(errors)


def collect_step_failure_summaries(artifact_dir: Path | None) -> list[str]:
    """Collect summary lines for non-success steps from exit_status.yaml files.

    Returns a list of formatted bullet lines, one per failed step.
    """
    from projects.core.library.ci import ExitStatus

    if not artifact_dir or not artifact_dir.is_dir():
        return []

    bullets = []
    for step_dir in sorted(artifact_dir.glob("*")):
        if not step_dir.is_dir() or step_dir.name.startswith(".") or step_dir.name == "lost+found":
            continue
        if step_dir.name == CI_METADATA_DIRNAME:
            continue

        try:
            exit_status = ExitStatus.load(step_dir / CI_METADATA_DIRNAME)
        except Exception:
            continue

        if not exit_status.records or exit_status.is_success:
            continue

        primary = exit_status.primary
        msg = f"• *{step_dir.name}* — `{primary.category}`"
        if primary.reason:
            msg += f" → _{primary.reason}_"
        bullets.append(msg)

    return bullets


class SlackNotificationProvider(ABC):
    """Abstract base for per-project Slack notification providers.

    Subclass this and implement the abstract methods to give a FORGE
    project its own Slack channel and message format.

    Token resolution uses the standard forge vault (topsail-bot.slack-token
    from psap-forge-notifications). Override ``get_slack_token()`` only if
    you need a different token.
    """

    def get_slack_token(self) -> str | None:
        """Resolve the Slack token from the standard forge notifications vault.

        Override this only if your project needs a different bot token.
        """
        try:
            token_path = vault.get_vault_content_path(
                "psap-forge-notifications", "topsail-bot.slack-token"
            )
        except RuntimeError:
            logger.warning("Vault not initialized, cannot resolve Slack token")
            return None

        if not token_path or not token_path.exists():
            logger.warning("Slack token not found in psap-forge-notifications vault")
            return None

        return token_path.read_text().strip()

    @abstractmethod
    def get_channel_id(self) -> str:
        """Return the Slack channel ID to post to."""

    @abstractmethod
    def format_message(self, context: NotificationContext) -> str:
        """Format the Slack message body."""

    def get_thread_anchor(self, context: NotificationContext) -> str | None:
        """Return the thread anchor text for grouping messages in a thread.

        Returns None when there is no PR, so the message is posted directly
        to the main channel without threading.
        """
        if context.pr_number:
            return f"Thread for {context.project_name} PR #{context.pr_number}"
        return None

    def get_thread_channel_message(self, context: NotificationContext, anchor: str) -> str:
        """Return the channel message that creates the thread.

        The anchor is used for searching; this message is what gets posted.
        Override to add extra info (e.g. PR title) to the visible message.
        """
        BASE_ANCHOR = f"🧵 {anchor}"

        if not context.pr_number:
            return BASE_ANCHOR

        from projects.core.library import config

        title = config.project.get_config("ci_job.gh.pr.title", None, print=False)
        if not title:
            return BASE_ANCHOR

        return f"{BASE_ANCHOR}\n```{title}```"

    def should_notify(self, context: NotificationContext) -> bool:
        """Return True if notification should be sent. Default: always notify."""
        return True

    def reply_broadcast(self, context: NotificationContext) -> bool:
        """Return True to also post the thread reply in the channel. Default: False."""
        return False

    def format_thread_replies(self, context: NotificationContext) -> list[str]:
        """Return messages to post as thread replies after the main message."""
        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _post_thread_replies(
        self,
        client,
        context: NotificationContext,
        *,
        thread_ts: str | None,
        channel_id: str,
        dry_run: bool,
    ) -> bool:
        replies = self.format_thread_replies(context)
        if not replies:
            return True

        for reply in replies:
            if dry_run:
                logger.info("Would post thread reply:\n%s", reply)
                continue

            _, ok = slack_api.send_message(
                client, message=reply, main_ts=thread_ts, channel_id=channel_id
            )
            if not ok:
                return False

        return True

    @staticmethod
    def _save_message_to_file(message: str, context: NotificationContext) -> None:
        """Persist the formatted Slack message to the artifact directory."""
        if not context.artifact_dir:
            return
        try:
            notification_file = context.artifact_dir / "SLACK-NOTIFICATION.txt"
            notification_file.write_text(message + "\n")
            logger.info("Wrote Slack notification to %s", notification_file)
        except Exception:
            logger.warning("Failed to save Slack notification to file", exc_info=True)

    # ------------------------------------------------------------------
    # Dispatch (not meant to be overridden in most cases)
    # ------------------------------------------------------------------

    def notify(self, context: NotificationContext, *, dry_run: bool = False) -> bool:
        """Execute the full notification flow.

        Returns True on success, False on failure.
        """
        if not self.should_notify(context):
            logger.info("Provider %s: should_notify returned False, skipping", type(self).__name__)
            return True

        token = self.get_slack_token()
        if not token:
            logger.warning("Provider %s: no Slack token available", type(self).__name__)
            return False

        channel_id = self.get_channel_id()
        message = self.format_message(context)
        self._save_message_to_file(message, context)

        if context.extra.get("_skip_notification"):
            logger.info("Provider %s: _skip_notification set, skipping", type(self).__name__)
            return True

        anchor = self.get_thread_anchor(context)

        client = slack_api.init_client(token)
        if not client:
            logger.error("Provider %s: failed to init Slack client", type(self).__name__)
            return False

        if anchor is None:
            if dry_run:
                logger.info("Would post channel message:\n%s", message)
                return self._post_thread_replies(
                    client, context, thread_ts=None, channel_id=channel_id, dry_run=True
                )

            msg_ts, ok = slack_api.send_message(client, message=message, channel_id=channel_id)
            if not ok:
                return False

            return self._post_thread_replies(
                client, context, thread_ts=msg_ts, channel_id=channel_id, dry_run=False
            )

        channel_msg_ts, _ = slack_api.search_channel_message(client, anchor, channel_id=channel_id)

        if not channel_msg_ts:
            channel_message = self.get_thread_channel_message(context, anchor)
            if dry_run:
                logger.info("Would post channel message: %s", channel_message)
            else:
                channel_msg_ts, ok = slack_api.send_message(
                    client, message=channel_message, channel_id=channel_id
                )
                if not ok:
                    return False

        if dry_run:
            logger.info("Would post thread message:\n%s", message)
            return self._post_thread_replies(
                client, context, thread_ts=channel_msg_ts, channel_id=channel_id, dry_run=True
            )

        _, ok = slack_api.send_message(
            client,
            message=message,
            main_ts=channel_msg_ts,
            channel_id=channel_id,
            reply_broadcast=self.reply_broadcast(context),
        )
        if not ok:
            return False

        return self._post_thread_replies(
            client, context, thread_ts=channel_msg_ts, channel_id=channel_id, dry_run=False
        )
