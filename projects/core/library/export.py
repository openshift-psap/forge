"""
Shared Caliper "artifacts export" CLI for FORGE project orchestration.

Registers a :mod:`click` subcommand that reads ``caliper`` from project config and runs
:func:`projects.caliper.orchestration.export.run_from_orchestration_config`.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
import yaml

from projects.caliper.orchestration.export import run_from_orchestration_config
from projects.core.library import ci as ci_lib
from projects.core.library import config, run
from projects.core.notifications import send

logger = logging.getLogger(__name__)


class FinishReason(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    OTHER = "other"


def _update_fjob_export_status(status: dict):
    """Update FournosJob status with export artifacts status."""
    if os.environ.get("FOURNOS_CI") != "true":
        return

    # Unset KUBECONFIG to use the pod SA access
    original_kubeconfig = os.environ.get("KUBECONFIG")
    if "KUBECONFIG" in os.environ:
        del os.environ["KUBECONFIG"]

    try:
        import json

        fjob_name = os.environ["FJOB_NAME"]
        namespace = os.environ["FOURNOS_WORKLOAD_NAMESPACE"]

        # Get current fjob status
        get_cmd = f"oc get fjob/{fjob_name} -n {namespace} -ojson"
        result = run.run(get_cmd, capture_stdout=True, check=False)

        if result.returncode != 0:
            logger.warning(f"Failed to get fjob/{fjob_name}")
            return

        fjob_data = json.loads(result.stdout)

        # Initialize status.engine.status if it doesn't exist
        if "status" not in fjob_data:
            fjob_data["status"] = {}
        if "engineStatus" not in fjob_data["status"]:
            fjob_data["status"]["engineStatus"] = {}
        if "forge" not in fjob_data["status"]["engineStatus"]:
            fjob_data["status"]["engineStatus"]["forge"] = {}
        if "status" not in fjob_data["status"]["engineStatus"]["forge"]:
            fjob_data["status"]["engineStatus"]["forge"]["status"] = {}

        # Update with export-artifacts status
        fjob_data["status"]["engineStatus"]["forge"]["exportArtifacts"] = status

        # Patch the fjob
        patch_data = {"status": fjob_data["status"]}
        patch_cmd = f"oc patch fjob/{fjob_name} -n {namespace} --type=merge --subresource=status -p '{json.dumps(patch_data)}'"

        patch_result = run.run(patch_cmd, check=False)
        if patch_result.returncode == 0:
            logger.info(f"Updated fjob/{fjob_name} status with export artifacts status")
        else:
            logger.warning(f"Failed to update fjob status: {patch_cmd}")

    except Exception as e:
        logger.warning(f"Failed to update fjob status: {e}")
    finally:
        # Restore KUBECONFIG if it was set
        if original_kubeconfig is not None:
            os.environ["KUBECONFIG"] = original_kubeconfig


def send_notification(status: dict[str, Any]) -> None:
    """Send job completion notifications based on caliper export status.

    Args:
        status: Caliper export status object containing backend results and metadata
    """
    # Extract notification parameters from status object
    project = _extract_project_from_status(status)
    operation = _extract_operation_from_status(status)
    finish_reason = _extract_finish_reason_from_status(status)
    duration_str = _extract_duration_from_status(status)

    # Apply minimal filtering logic
    if _should_skip_notification(project, operation, finish_reason):
        logger.info(f"Skipping notification for {project} {operation}")
        return

    # Send actual notifications
    logger.info(f"Sending notification: {project} {operation} {finish_reason}{duration_str}")

    notification_status = f"Export artifacts for '{project}' {('succeeded' if finish_reason == FinishReason.SUCCESS else 'failed')}{duration_str}"

    # Enable GitHub and Slack notifications by default
    github_notifications = True
    slack_notifications = True
    dry_run = os.environ.get("FORGE_NOTIFICATION_DRY_RUN", "false").lower() == "true"

    # Send the notification
    notification_failed = send.send_job_completion_notification(
        finish_reason=finish_reason,
        status=notification_status,
        github=github_notifications,
        slack=slack_notifications,
        dry_run=dry_run,
    )

    if notification_failed:
        logger.warning("Some notifications failed to send")
    else:
        logger.info("Notifications sent successfully")


def _extract_project_from_status(status: dict[str, Any]) -> str:
    """Extract project name from status object or environment."""
    # Try to get project from environment variables
    project = os.environ.get("PROJECT_NAME")
    if project:
        return project

    # Fallback to JOB_NAME parsing (common in CI environments)
    job_name = os.environ.get("JOB_NAME", "")
    if job_name and "-" in job_name:
        # Extract project from job name pattern like "project-operation-variant"
        return job_name.split("-")[0]

    return "unknown"


def _extract_operation_from_status(status: dict[str, Any]) -> str:
    """Extract operation name from status object."""
    return "export-artifacts"


def _extract_finish_reason_from_status(status: dict[str, Any]) -> FinishReason:
    """Extract finish reason from status object."""
    # Check if any backend failed in the status
    if not status:
        return FinishReason.ERROR

    # Look for backend results
    backends = status.get("backends", {})
    for backend_name, backend_result in backends.items():
        if backend_result.get("success") is False:
            logger.info(f"Backend {backend_name} failed, marking as error")
            return FinishReason.ERROR

    return FinishReason.SUCCESS


def _extract_duration_from_status(status: dict[str, Any]) -> str:
    """Extract duration from status object."""
    # Look for duration in status
    duration = status.get("duration")
    if duration:
        return f" after {duration}"
    return ""


def _should_skip_notification(project: str, operation: str, finish_reason: FinishReason) -> bool:
    """Apply minimal filtering logic to determine if notification should be skipped."""
    # Minimal filtering - no special cases for now
    return False


def run_caliper_orchestration_export(*, artifact_directory: Path | None):
    """Set optional ``caliper.export.from`` and run orchestration export."""

    if artifact_directory is None and "ARTIFACT_BASE_DIR" in os.environ:
        artifact_directory = os.environ["ARTIFACT_BASE_DIR"]

    if artifact_directory is not None:
        config.project.set_config("caliper.export.from", str(artifact_directory))

    # Use FJOB_NAME as fallback for mlflow run_name if not configured
    run_name = config.project.get_config(
        "caliper.export.backend.mlflow.config.run_name", None, print=False, warn=False
    )
    if run_name is None and "FJOB_NAME" in os.environ:
        config.project.set_config(
            "caliper.export.backend.mlflow.config.run_name", os.environ["FJOB_NAME"], print=False
        )

    caliper_cfg = config.project.get_config("caliper", print=False)

    return run_from_orchestration_config(caliper_cfg)


@click.command("export-artifacts")
@click.option(
    "--artifact-directory",
    "artifact_directory",
    type=click.Path(path_type=Path, exists=False, file_okay=True, dir_okay=True),
    default=None,
    help="If set, overrides caliper.export.from (artifact root directory).",
)
@click.pass_context
@ci_lib.safe_ci_command
def caliper_export_entrypoint(_ctx, artifact_directory: Path | None):
    """Export the file artifacts."""

    status = run_caliper_orchestration_export(artifact_directory=artifact_directory)
    logger.info("Export status:\n" + yaml.dump(status, indent=4))

    # Update fjob status with export results
    _update_fjob_export_status(status)

    # Send completion notifications
    try:
        send_notification(status)
    except Exception as e:
        logger.warning(f"Failed to send notifications: {e}")
        # Don't fail the entire job if notifications fail

    return 0
