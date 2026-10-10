"""RHAIIS Slack notification provider for pipeline completion."""

from __future__ import annotations

import logging
import os

from projects.core.library import env
from projects.core.notifications.provider import (
    NotificationContext,
    SlackNotificationProvider,
    collect_failure_errors,
    collect_notification_files,
    collect_step_failure_summaries,
)

logger = logging.getLogger(__name__)


class RhaiisSlackProvider(SlackNotificationProvider):
    """Sends RHAIIS pipeline completion notifications to Slack."""

    def get_channel_id(self) -> str:
        from projects.core.library import config

        return config.project.get_config("tests.rhaiis.slack_channel_id", "")

    def should_notify(self, context: NotificationContext) -> bool:
        from projects.core.library import config

        failed = context.finish_reason in ("failed", "export failed", "aborted")
        if failed:
            return True

        return config.project.get_config("tests.rhaiis.slack_notify_always", False)

    def reply_broadcast(self, context: NotificationContext) -> bool:
        return False

    def format_message(self, context: NotificationContext) -> str:
        from projects.core.library import config
        from projects.rhaiis.orchestration import runtime_config
        from projects.rhaiis.postprocess.regression import (
            _build_dashboard_url,
            _build_mlflow_run_url,
            _format_owner_line,
            _format_slack_user_line,
        )

        status = context.status or {}
        success = status.get("success", False)
        failed = context.finish_reason in ("failed", "export failed", "aborted")

        model_key = config.project.get_config("tests.rhaiis.model_key", "unknown")
        try:
            model_cfg = runtime_config.get_model(model_key)
            model_name = model_cfg.get("hf_model_id", model_key)
        except Exception:
            model_name = model_key

        accelerator = runtime_config.get_accelerator()
        engine = runtime_config.get_engine()
        slack_user = config.project.get_config("tests.rhaiis.slack_user", "")
        owner = config.project.get_config("ci_job.owner", "") or ""
        version = config.project.get_config("tests.rhaiis.version", "")
        cluster = config.project.get_config("rhaiis.cluster_tag", "")
        workload_keys = config.project.get_config("tests.rhaiis.workload_keys", [])
        job_id = os.environ.get("FJOB_NAME", "")

        engine_defaults = runtime_config.get_engine_args(engine)
        ea = runtime_config.merge_engine_args(engine_defaults, model_cfg, {}, engine)
        tp = ea.get("tensor-parallel-size") or ea.get("tp-size") or ea.get("tp_size") or ""
        dp = ea.get("data-parallel-size") or ea.get("dp-size") or ""

        parallelism_parts = []
        if tp:
            parallelism_parts.append(f"TP={tp}")
        if dp:
            parallelism_parts.append(f"DP={dp}")
        parallelism_line = (
            f"*Parallelism:* {', '.join(parallelism_parts)}\n" if parallelism_parts else ""
        )

        user_line = _format_slack_user_line(slack_user)
        owner_line = _format_owner_line(owner)
        engine_line = f"*Engine:* {engine}\n" if engine else ""
        version_line = f"*Version:* {version}\n" if version else ""
        cluster_line = f"*Cluster:* {cluster}\n" if cluster else ""
        workloads_line = f"*Workloads:* {', '.join(workload_keys)}\n" if workload_keys else ""

        dashboard_url = _build_dashboard_url(
            model=model_name,
            accelerator=accelerator,
            current_version=version,
            profiles=workload_keys or None,
            tp=str(tp) if tp else "",
        )
        dashboard_line = f"*Dashboard:* <{dashboard_url}|View Dashboard>\n"

        mlflow_url = _build_mlflow_run_url()
        mlflow_line = f"*MLflow:* <{mlflow_url}|View Run>\n" if mlflow_url else ""

        if failed or not success:
            emoji = ":x:"
            title = "RHAIIS Pipeline Failed"
        else:
            emoji = ":white_check_mark:"
            title = "RHAIIS Pipeline Succeeded"

        test_desc, _regular_notifs, _failure_reviews = collect_notification_files(
            env.BASE_ARTIFACT_DIR.parent
        )

        step_failures = collect_step_failure_summaries(env.BASE_ARTIFACT_DIR.parent)

        parts = [
            f"{emoji} *{title}*\n",
        ]

        if step_failures:
            parts.extend(step_failures)
            parts.append("")

        if test_desc:
            parts.append(test_desc)
            parts.append("")

        parts.append(
            f"{user_line}"
            f"{owner_line}"
            f"*Job:* `{job_id}`\n"
            f"*Model:* {model_name}\n"
            f"*Accelerator:* {accelerator}\n"
            f"{parallelism_line}"
            f"{engine_line}"
            f"{version_line}"
            f"{cluster_line}"
            f"{workloads_line}"
            f"{dashboard_line}"
            f"{mlflow_line}"
        )

        return "\n".join(parts).rstrip("\n")

    def format_thread_replies(self, context: NotificationContext) -> list[str]:
        _test_desc, regular_notifs, failure_reviews = collect_notification_files(
            env.BASE_ARTIFACT_DIR.parent
        )

        replies = []

        if regular_notifs:
            notif_parts = ["*Notifications:*"]
            for notif in regular_notifs:
                notif_parts.append("")
                notif_parts.append(notif)
            replies.append("\n".join(notif_parts).rstrip("\n"))

        status = context.status or {}
        success = status.get("success", False)
        failed = context.finish_reason in ("failed", "export failed", "aborted")

        if failed or not success:
            failure_search_dir = env.BASE_ARTIFACT_DIR.parent
            logger.info(
                f"Searching for failure errors in {failure_search_dir} "
                f"(BASE_ARTIFACT_DIR={env.BASE_ARTIFACT_DIR})"
            )
            collected_errors = collect_failure_errors(failure_search_dir)
            if not collected_errors:
                logger.warning(
                    f"No failure errors collected, falling back to "
                    f"finish_reason={context.finish_reason!r}"
                )
            error_text = "```\n" + (collected_errors or context.finish_reason) + "\n```"

            error_parts = ["*Error:*"]
            for review in failure_reviews:
                error_parts.append("")
                error_parts.append(review)
                error_parts.append("")
            error_parts.append(error_text)
            replies.append("\n".join(error_parts).rstrip("\n"))

        return replies
