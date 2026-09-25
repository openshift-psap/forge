"""
Notification handling for Caliper export operations.

This module provides notification functionality for export completion,
including GitHub notifications and Slack notifications via project providers.
"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.constants import METADATA_FILE
from projects.caliper.engine.kpi.dataclasses import CaliperTestMetadata
from projects.caliper.orchestration.censoring import censor_text
from projects.caliper.orchestration.postprocess import POSTPROCESS_STATUS_FILENAME
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.core.library.step_status import StepStatus
from projects.core.notifications.provider import NotificationContext
from projects.core.notifications.send import send_notification

logger = logging.getLogger(__name__)

COMPLETION_NOTIFICATION_FILENAME = "COMPLETION-NOTIFICATION.md"


def _calculate_duration(start_time: str, end_time: str) -> str:
    """Calculate duration between two ISO timestamps and return formatted string."""
    try:
        from datetime import datetime

        # Parse ISO timestamps (handle both with and without 'Z' suffix)
        start_time_clean = (
            start_time.replace("Z", "+00:00") if start_time.endswith("Z") else start_time
        )
        end_time_clean = end_time.replace("Z", "+00:00") if end_time.endswith("Z") else end_time

        start_dt = datetime.fromisoformat(start_time_clean)
        end_dt = datetime.fromisoformat(end_time_clean)

        duration = end_dt - start_dt
        total_seconds = duration.total_seconds()

        if total_seconds < 60:
            return f"{total_seconds:.1f}s"
        elif total_seconds < 3600:
            minutes = total_seconds / 60
            return f"{minutes:.1f}m"
        else:
            hours = total_seconds / 3600
            return f"{hours:.1f}h"

    except Exception as e:
        logger.warning(f"Failed to calculate duration between {start_time} and {end_time}: {e}")
        return f"{start_time} → {end_time}"


@dataclass
class BackendResult:
    """Result from a single backend export."""

    success: bool = False
    run_id: str | None = None
    experiment_url: str | None = None
    run_url: str | None = None
    tracking_uri: str | None = None
    detail: str | None = None
    status: str | None = None
    child_runs: list[dict[str, str]] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackendResult":
        """Create BackendResult from raw dict."""
        # Infer success from status if not explicitly provided
        success = data.get("success")
        if success is None:
            status = data.get("status", "")
            success = status == "success"

        return cls(
            success=success,
            run_id=data.get("run_id"),
            experiment_url=data.get("experiment_url"),
            run_url=data.get("run_url"),
            tracking_uri=data.get("tracking_uri"),
            detail=data.get("detail"),
            status=data.get("status"),
            child_runs=data.get("child_runs"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result = {"success": self.success}
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.experiment_url is not None:
            result["experiment_url"] = self.experiment_url
        if self.run_url is not None:
            result["run_url"] = self.run_url
        if self.tracking_uri is not None:
            result["tracking_uri"] = self.tracking_uri
        if self.detail is not None:
            result["detail"] = self.detail
        if self.status is not None:
            result["status"] = self.status
        if self.child_runs is not None:
            result["child_runs"] = self.child_runs
        return result


@dataclass
class CaliperArtifactsExport:
    """Caliper artifacts export information."""

    version: int = 1
    backends: dict[str, BackendResult] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaliperArtifactsExport":
        """Create CaliperArtifactsExport from raw dict."""
        backends_data = data.get("backends", {})
        backends = {}
        for backend_name, backend_data in backends_data.items():
            backends[backend_name] = BackendResult.from_dict(backend_data)

        return cls(
            version=data.get("version", 1),
            backends=backends if backends else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result = {"version": self.version}
        if self.backends:
            backends_dict = {}
            for backend_name, backend_result in self.backends.items():
                backends_dict[backend_name] = backend_result.to_dict()
            result["backends"] = backends_dict
        return result


@dataclass
class TestPhase:
    """Test execution phase information."""

    phase: str
    message: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestPhase":
        """Create TestPhase from raw dict."""
        return cls(
            phase=data.get("phase", "UNKNOWN"),
            message=data.get("message", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "phase": self.phase,
            "message": self.message,
        }


@dataclass
class JobShutdown:
    """Job shutdown/abort information."""

    is_aborted: bool = False
    shutdown_value: str | None = None
    shutdown_detected: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobShutdown":
        """Create JobShutdown from raw dict."""
        return cls(
            is_aborted=data.get("is_aborted", False),
            shutdown_value=data.get("shutdown_value"),
            shutdown_detected=data.get("shutdown_detected", False),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result = {
            "is_aborted": self.is_aborted,
            "shutdown_detected": self.shutdown_detected,
        }
        if self.shutdown_value is not None:
            result["shutdown_value"] = self.shutdown_value
        return result


@dataclass
class ExportStatus:
    """Dataclass for caliper export status."""

    success: bool
    final_status: str
    censoring_occurred: bool = False
    duration: str | None = None
    caliper_artifacts_export: CaliperArtifactsExport | None = None
    test_phase: TestPhase | None = None
    job_shutdown: JobShutdown | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExportStatus":
        """Create ExportStatus from raw dict, providing proper defaults."""
        # Convert nested structures to typed objects first
        caliper_export = None
        if data.get("caliper_artifacts_export"):
            caliper_export = CaliperArtifactsExport.from_dict(data["caliper_artifacts_export"])

        test_phase = None
        if data.get("test_phase"):
            test_phase = TestPhase.from_dict(data["test_phase"])

        job_shutdown = None
        if data.get("job_shutdown"):
            job_shutdown = JobShutdown.from_dict(data["job_shutdown"])

        # Extract success from various possible sources
        success = data.get("success")
        final_status = data.get("final_status", "")

        if success is None:
            # Try to determine success from final_status first
            if final_status in ("success", "completed"):
                success = True
            elif final_status in ("failed", "failure", "error"):
                success = False
            elif caliper_export and caliper_export.backends:
                # Infer success from backend results if no explicit status
                backend_successes = []
                for backend_result in caliper_export.backends.values():
                    backend_successes.append(backend_result.success)

                # Overall success if all backends succeeded
                success = all(backend_successes) if backend_successes else False

                # Set final_status based on inferred success if it wasn't set
                if not final_status or final_status == "unknown":
                    final_status = "success" if success else "failed"
            else:
                # Default to failed if we can't determine success
                success = False
                final_status = final_status or "unknown"

        return cls(
            success=success,
            final_status=final_status,
            censoring_occurred=data.get("censoring_occurred", False),
            duration=data.get("duration"),
            caliper_artifacts_export=caliper_export,
            test_phase=test_phase,
            job_shutdown=job_shutdown,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert back to dict for compatibility."""
        result = {
            "success": self.success,
            "final_status": self.final_status,
            "censoring_occurred": self.censoring_occurred,
        }

        if self.duration is not None:
            result["duration"] = self.duration
        if self.caliper_artifacts_export is not None:
            result["caliper_artifacts_export"] = self.caliper_artifacts_export.to_dict()
        if self.test_phase is not None:
            result["test_phase"] = self.test_phase.to_dict()
        if self.job_shutdown is not None:
            result["job_shutdown"] = self.job_shutdown.to_dict()

        return result


def _censor_notification_text(text: str, verbose: bool = False) -> str:
    """
    Censor sensitive content in notification text using caliper orchestration.

    Args:
        text: The notification text to censor
        verbose: Enable verbose logging

    Returns:
        Censored notification text with sensitive content replaced

    Raises:
        Exception: If vault secret retrieval fails, preventing notification delivery
    """
    try:
        return censor_text(text, verbose=verbose)
    except Exception as e:
        logger.error(f"Censoring failed during notification preparation: {e}")
        # Re-raise to abort GitHub and Slack notification delivery
        # rather than sending potentially uncensored text
        raise


def _send_completion_message(
    notification_status: str,
    dry_run: bool,
) -> bool:
    """Send the completion notification to GitHub and Jira via send_notification.

    Args:
        notification_status: Censored markdown notification content
        dry_run: If True, log but don't send

    Returns:
        bool: True if all notifications succeeded
    """
    try:
        notification_vault = None
        try:
            notification_config = config.project.get_config("caliper.export.notifications", {})
            notification_vault = notification_config.get("vault")
            if notification_vault:
                logger.info(f"Using notification vault from config: {notification_vault}")
        except Exception as e:
            logger.warning(f"Failed to get notification vault from config: {e}")

        success = send_notification(
            message=notification_status,
            github=True,
            dry_run=dry_run,
            notification_vault=notification_vault,
        )
        if success:
            logger.info("Successfully sent completion notifications")
        else:
            logger.error("Completion notification sending failed")

        return success

    except Exception as e:
        logger.error(f"Failed to send completion notifications: {e}")
        return False


def _send_slack_provider_notification(
    notification_provider,
    status: ExportStatus,
    finish_reason: str,
    project: str,
    dry_run: bool,
) -> bool:
    """Send notification via the per-project Slack notification provider.

    Args:
        notification_provider: SlackNotificationProvider instance
        status: Export status dataclass (will be censored before sending)
        finish_reason: Reason string for the notification context
        project: Project name
        dry_run: If True, log but don't send

    Returns:
        bool: True if notification succeeded
    """
    if not notification_provider:
        logger.info("No slack notification provider, nothing to send.")
        return True

    if dry_run:
        logger.info(
            f"DRY RUN: Would send per-project Slack notification with {notification_provider}"
        )
        return True

    try:
        artifact_dir = env.ARTIFACT_DIR

        try:
            censored_status = yaml.safe_load(
                _censor_notification_text(yaml.dump(status.to_dict()), verbose=False)
            )
        except Exception as e:
            logger.error(f"Slack notification censoring failed, aborting Slack notification: {e}")
            return False

        context = NotificationContext(
            status=censored_status,
            finish_reason=str(finish_reason),
            project_name=project or "unknown",
            pr_number=os.environ.get("PULL_NUMBER"),
            job_type=os.environ.get("JOB_TYPE"),
            artifact_dir=artifact_dir,
        )
        ok = notification_provider.notify(context)
        if ok:
            logger.info("Successfully sent per-project Slack notification")
        else:
            logger.warning("Per-project Slack notification failed")

        return ok

    except Exception as e:
        logger.warning(f"Failed to send per-project Slack notification: {e}")
        return False


def send_completion_notifications(
    artifact_dir: Path,
    status: ExportStatus,
    notification_provider=None,
    dry_run: bool = False,
):
    """Send job completion notifications based on caliper export status.

    Args:
        artifact_dir: Directory to browse to find the artifacts
        status: Caliper export status dataclass
        notification_provider: Optional per-project SlackNotificationProvider instance
        dry_run: If True, only build and log notification content without sending

    Returns:
        bool: True if notifications were sent successfully, False otherwise
    """

    project = config.project.get_config("project.name")

    try:
        step_status = _get_overall_step_status(artifact_dir)
        if step_status == StepStatus.FAILURE:
            logger.info("Step failure detected from exit_status.yaml files")
    except Exception as e:
        logger.warning(f"Failed to check step exit statuses for notification: {e}")
        step_status = None

    finish_reason = _extract_finish_reason_from_status(status, step_status)

    success = True
    success &= _send_detailed_completion_notification(
        artifact_dir,
        status,
        finish_reason,
        project,
        step_status,
        dry_run,
    )

    success &= _send_slack_provider_notification(
        notification_provider, status, finish_reason, project, dry_run
    )

    return success


def _send_detailed_completion_notification(
    artifact_dir: Path,
    status: ExportStatus,
    finish_reason: str,
    project: str,
    step_status,
    dry_run: bool = False,
) -> bool:

    notification_status, notification_success = _build_enhanced_notification(
        artifact_dir, project, finish_reason, status, step_status
    )

    try:
        notification_status = _censor_notification_text(notification_status, verbose=dry_run)
    except Exception as e:
        logger.error(f"Notification censoring failed, aborting notification delivery: {e}")
        return False

    if dry_run:
        logger.info("DRY RUN: Would send notification")
        logger.info(f"DRY RUN: Notification content:\n{notification_status}")
    else:
        logger.info("Sending notification ...")

    notification_file = env.ARTIFACT_DIR / COMPLETION_NOTIFICATION_FILENAME
    notification_file.write_text(notification_status + "\n")
    logger.info(f"Wrote export notification file {notification_file}")

    return notification_success and _send_completion_message(notification_status, dry_run)


def _get_project_and_args(project: str, artifact_dir: Path | None) -> tuple[str, str, str]:
    """Extract project name, args, and job info from fournos job or config."""
    fjob_project = project
    fjob_args_str = ""
    job_info_line = ""

    try:
        metadata_dir = ci_lib.get_ci_metadata_dir(base_ci_dir=artifact_dir, any_level=True)
        if not metadata_dir:
            return fjob_project, fjob_args_str, job_info_line

        fournos_fjob_path = metadata_dir / "fournos_fjob.yaml"
        if not fournos_fjob_path.exists():
            return fjob_project, fjob_args_str, job_info_line

        with open(fournos_fjob_path, encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f)

        # Get project name and args from executionEngine.forge configuration
        spec = fjob_data.get("spec", {})
        execution_engine = spec.get("executionEngine", {})
        forge_config = execution_engine.get("forge", {})
        fjob_project = forge_config.get("project", project)

        # Get forge args from the executionEngine.forge.args
        forge_args = forge_config.get("args", [])
        if forge_args:
            fjob_args_str = " ".join(forge_args)

        # Get name and displayName for the second line
        fjob_name = fjob_data.get("metadata", {}).get("name", "name not found")
        display_name = spec.get("displayName", "displayName not set")

        job_info_line = f"> `{fjob_name}` -- _{display_name}_"

        logger.info(
            f"Extracted fjob info - project: {fjob_project}, args: {fjob_args_str}, job_info: {job_info_line}"
        )
    except Exception as e:
        logger.warning(f"Failed to read fournos job for project/args: {e}")

    return fjob_project, fjob_args_str, job_info_line


def _extract_finish_reason_from_status(
    status: ExportStatus, step_status: StepStatus | None = None
) -> str:
    """Extract finish reason from status, including step exit status checking."""
    if status.job_shutdown and status.job_shutdown.is_aborted:
        return "aborted"
    elif not status.success:
        return "export failed"
    elif step_status == StepStatus.FAILURE:
        return "failed"
    elif status.censoring_occurred:
        return "completed with censoring"
    else:
        return "completed"


def _build_enhanced_notification(
    artifact_dir: Path | None,
    project: str,
    finish_reason: str,
    status: ExportStatus,
    step_status: StepStatus | None = None,
) -> tuple[str, bool]:
    """Build enhanced notification with fournos job config and artifact links."""
    fjob_project, fjob_args_str, job_info_line = _get_project_and_args(project, artifact_dir)

    success = status.success
    censoring_occurred = status.censoring_occurred

    # Override success if step failures detected
    step_failure_detected = step_status == StepStatus.FAILURE
    if step_failure_detected:
        success = False  # Override success if any step failed
        logger.info(
            "Step failure detected from exit_status.yaml files - overriding success to False"
        )

    logger.info(
        f"Building notification - success={success}, censoring_occurred={censoring_occurred}, finish_reason='{finish_reason}', step_failure_detected={step_failure_detected}"
    )
    status_emoji = "�"
    status_reason = "unknown"
    status_what = "status unknown"
    # Determine status emoji with abort taking precedence
    if status.job_shutdown and status.job_shutdown.is_aborted:
        status_emoji = "⛔"
        status_reason = "user abort"
        status_what = "was aborted"
    elif step_failure_detected:
        status_emoji = "❌"  # Step failures take precedence over export success
        status_reason = "pipeline step failure"
        status_what = "failed"
    elif not success:
        status_emoji = "❌"
        status_reason = "export failure"
        status_what = "completed"
    elif censoring_occurred:
        status_emoji = "⚠️"
        status_reason = "censoring detected"
        status_what = "completed"
    else:
        status_emoji = "✅"
        status_reason = None
        status_what = "completed with success"

    # Add total duration to base status
    total_duration = _read_total_duration(artifact_dir)
    duration_suffix = f" `{total_duration}`" if total_duration else ""

    # Format the execution line with project and args separated
    if fjob_args_str:
        execution_text = f"Execution of `{fjob_project}` | `{fjob_args_str}`"
    else:
        execution_text = f"Execution of `{fjob_project}`"

    status_reason_str = f" (`{status_reason}`)" if status_reason else ""
    base_status = f"{status_emoji} {execution_text} {status_what}{status_reason_str} after {duration_suffix} {status_emoji}"
    notification_parts = [base_status]

    # Add job info line (name and displayName) if available
    if job_info_line:
        notification_parts.append(job_info_line)

    # Add job abort message right below overall status if applicable
    shutdown_status = status.job_shutdown
    if shutdown_status and shutdown_status.is_aborted:
        notification_parts += ["", "---"]
        shutdown_value = shutdown_status.shutdown_value or "Stop"
        notification_parts.append(f"⛔ **JOB ABORTED** - `spec.shutdown={shutdown_value}`")

    execution_engine_config = _get_execution_engine_config(artifact_dir)
    if execution_engine_config:
        notification_parts += ["", "---"]
        notification_parts.append("**Execution Engine Configuration**")
        notification_parts.append(execution_engine_config)

    notification_success = True

    # Extract artifact links (with error handling)
    try:
        artifact_links, mlflow_run_url = _extract_artifact_links(status)
    except Exception as e:
        logger.exception(f"Failed to extract artifact links: {e}")
        artifact_links, mlflow_run_url = [], None
        notification_success = False

    # Extract test status section (with error handling)
    try:
        test_status_section = _extract_test_status_section(status)
    except Exception as e:
        logger.exception(f"Failed to extract test status: {e}")
        test_status_section = None

    # Extract step status (with error handling)
    try:
        step_status = _get_step_status_section(artifact_dir, mlflow_run_url)
    except Exception as e:
        logger.exception(f"Failed to extract step status: {e}")
        step_status = None

    # Extract postprocess status (with error handling - don't let this break the notification)
    try:
        postprocess_status_links = _get_postprocess_status_links(artifact_dir, mlflow_run_url)
    except Exception as e:
        logger.exception(f"Failed to extract postprocess status: {e}")
        postprocess_status_links = []

    # Extract censoring report (with error handling)
    try:
        censoring_report_section = _get_censoring_report_section(artifact_dir)
    except Exception as e:
        logger.exception(f"Failed to extract censoring report: {e}")
        censoring_report_section = None

    # Build notification sections
    if test_status_section:
        notification_parts += ["", "---"]
        notification_parts.extend(test_status_section)

    if artifact_links:
        notification_parts += ["", "---"]
        notification_parts.append("**MLFlow links**")
        notification_parts.extend(artifact_links)
    else:
        if notification_success:
            notification_parts.append("**MLFlow links:** No direct links available")
        else:
            notification_parts.append("**MLFlow links:** Error extracting links")

    if step_status:
        notification_parts += ["", "---"]
        notification_parts.append("**Pipeline Step Details**")
        for link in step_status:
            notification_parts.append(link)

    if postprocess_status_links:
        notification_parts += ["", "---"]
        notification_parts.extend(postprocess_status_links)

    if censoring_report_section:
        notification_parts += ["", "---"]
        notification_parts.append("**Censoring Report**")
        notification_parts.extend(censoring_report_section)

    return "\n".join(notification_parts), notification_success


def _get_censoring_report_section(artifact_dir: Path | None) -> list[str] | None:
    """
    Parse censoring_report.yaml if it exists and return notification section.

    Returns:
        List of notification lines or None if no report exists
    """
    if not artifact_dir:
        return None

    # Look for censoring_report.yaml at the top level of the step files
    censoring_report_path = artifact_dir / "censoring_report.yaml"

    if not censoring_report_path.exists():
        return None

    try:
        with open(censoring_report_path) as f:
            report_data = yaml.safe_load(f)

        if not report_data:
            return None

        notification_lines = []

        # Show number of files scanned
        total_files = report_data.get("total_files", 0)
        clean_files = report_data.get("clean_files", 0)
        safe_censored_files = report_data.get("safe_censored_files", 0)
        censored_files = report_data.get("censored_files", 0)  # Only unexpected now

        notification_lines.append(f"📊 **Files scanned:** {total_files}")
        notification_lines.append(
            f"✅ Clean: {clean_files}, 🔒 Safe replacements: {safe_censored_files}"
        )

        # Show unexpected censoring issues if any
        censored_by_reason = report_data.get("censored_by_reason", {})
        if censored_files > 0 and censored_by_reason:
            notification_lines.append(
                f"⚠️ **Unexpected sensitive content:** {censored_files} file(s)"
            )
            notification_lines.append("")

            for reason, files in censored_by_reason.items():
                # Shorten long reasons for notification
                short_reason = reason
                if len(reason) > 60:
                    short_reason = reason[:57] + "..."

                file_count = len(files)
                if file_count <= 3:
                    # Show individual files for small counts
                    file_list = ", ".join([f"`{f}`" for f in files])
                    notification_lines.append(f"* {short_reason}: {file_list}")
                else:
                    # Show count for large lists
                    first_files = ", ".join([f"`{f}`" for f in files[:2]])
                    notification_lines.append(
                        f"* {short_reason}: {first_files} and {file_count - 2} more"
                    )
        elif censored_files == 0:
            notification_lines.append("✅ **No unexpected sensitive content found**")

        return notification_lines

    except Exception as e:
        logger.warning(f"Failed to parse censoring report: {e}")
        return [f"⚠️ **Censoring report parsing failed:** {e}"]


def _get_execution_engine_config(artifact_dir: Path) -> str | None:
    """Read and format execution engine configuration."""
    try:
        metadata_dir = ci_lib.get_ci_metadata_dir(artifact_dir, any_level=True)
        fournos_fjob_path = metadata_dir / "fournos_fjob.yaml"
        if not fournos_fjob_path.exists():
            return f"* FournosJob not found at `{fournos_fjob_path}`"

        with open(fournos_fjob_path, encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f)

        execution_engine = fjob_data.get("spec", {}).get("executionEngine", {})

        spec = fjob_data.get("spec", {})

        config = {}
        config["executionEngine"] = execution_engine
        config["exclusive"] = spec.get("exclusive")
        config["owner"] = spec.get("owner")
        config["pipeline"] = spec.get("pipeline")

        if cluster := spec.get("cluster"):
            config["cluster"] = cluster

        if clusterless := spec.get("clusterless"):
            config["clusterless"] = clusterless

        if hardware := spec.get("hardware"):
            config["hardware"] = hardware

        config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=True)
        return f"```yaml\n{config_yaml.strip()}\n```"
    except Exception as e:
        return f"* Failed to read fournos job config: `{e}`"


def _extract_test_status_section(status: ExportStatus) -> list[str] | None:
    """Extract test status section from status."""
    test_phase = status.test_phase
    if not test_phase:
        return None

    phase = test_phase.phase.upper()
    message = test_phase.message
    test_status_emoji = "✅" if phase == "PASSED" else "❌" if phase == "FAILED" else "⚠️"

    return [
        f"**{test_status_emoji} Test Status: {phase} {test_status_emoji}**",
        f"**Message:** {message}",
    ]


def _get_step_status_section(artifact_dir: Path | None, mlflow_run_url: str | None) -> list[str]:
    """Get step status section with links."""
    if not artifact_dir or not artifact_dir.exists():
        return []

    step_status = []
    for step_dir in sorted(artifact_dir.glob("*")):
        if not step_dir.is_dir() or step_dir.name.startswith(".") or step_dir.name == "lost+found":
            continue

        step_name = step_dir.name

        # Skip special directories
        if step_name == CI_METADATA_DIRNAME:
            continue

        exit_status_file = step_dir / CI_METADATA_DIRNAME / "exit_status.yaml"
        exit_status_emoji = "❓"

        if exit_status_file.exists():
            try:
                with open(exit_status_file, encoding="utf-8") as f:
                    exit_data = yaml.safe_load(f)
                exit_code = exit_data.get("return_code", 999)
                if exit_code == 0:
                    exit_status_emoji = "✅"
                else:
                    exit_status_emoji = "❌"
            except Exception:
                exit_status_emoji = "❓"

        if step_dir.name.endswith("__export-artifacts"):
            exit_status_emoji = "📤"

        # Count ERROR and WARNING messages in run.log
        log_counts = _count_log_messages(step_dir)
        log_summary = _format_log_summary(log_counts)

        # Read step duration
        duration_str = _read_step_duration(step_dir)
        duration_suffix = f" `{duration_str}`" if duration_str else ""

        # Create step title - linked if MLflow URL available, plain-text otherwise

        mlflow_log_link = _create_mlflow_file_link(step_name, mlflow_run_url, step_dir / "run.log")
        step_title = f"#### {exit_status_emoji} {mlflow_log_link}{duration_suffix}{log_summary}"

        step_status.append(step_title)

        step_status.extend(_process_notification_files(step_dir))

        step_details = _process_step_details(step_dir, mlflow_run_url)
        if step_details:
            step_status.extend(step_details)

    return step_status


def _count_log_messages(step_dir: Path) -> dict[str, int]:
    """Count ERROR and WARNING messages in run.log file.

    Args:
        step_dir: Directory containing the run.log file

    Returns:
        Dict with 'errors' and 'warnings' counts
    """
    log_file = step_dir / "run.log"
    counts = {"errors": 0, "warnings": 0}

    if not log_file.exists():
        return counts

    try:
        with open(log_file, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ERROR:"):
                    counts["errors"] += 1
                elif line.startswith("WARNING:"):
                    counts["warnings"] += 1
    except Exception as e:
        logger.warning(f"Failed to read log file {log_file}: {e}")

    return counts


def _format_log_summary(log_counts: dict[str, int]) -> str:
    """Format log counts for display in step title.

    Args:
        log_counts: Dict with 'errors' and 'warnings' counts

    Returns:
        Formatted string to append to step title, or empty string if no issues
    """
    errors = log_counts.get("errors", 0)
    warnings = log_counts.get("warnings", 0)

    if errors == 0 and warnings == 0:
        return ""

    parts = []
    if errors > 0:
        parts.append(f"🔴 {errors}E")
    if warnings > 0:
        parts.append(f"🟡 {warnings}W")

    return f" ({', '.join(parts)})"


def _get_postprocess_status_links(
    artifact_dir: Path | None, mlflow_run_url: str | None
) -> list[str]:
    """Get postprocess status links."""
    if not artifact_dir or not artifact_dir.exists():
        return []

    step_log_links = []

    # Look for postprocess results in step directories
    step_dirs = sorted(artifact_dir.glob("*"))

    for step_dir in step_dirs:
        if not step_dir.is_dir() or step_dir.name.startswith("."):
            continue

        step_name = step_dir.name

        # Skip special directories
        if step_name == CI_METADATA_DIRNAME:
            continue

        try:
            # Search for postprocess status files recursively within this step directory
            step_postprocess_files = list(step_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"))

            if not step_postprocess_files:
                logger.info(
                    f"Postprocess status - {step_name}: no {POSTPROCESS_STATUS_FILENAME} files found"
                )
                continue

            # Import notification functions from caliper (inside function to avoid circular imports)
            from projects.caliper.orchestration.notification import (
                format_postprocess_status_notification,
                parse_postprocess_status,
            )

            # Process ALL postprocess status files found
            for postprocess_status_file in step_postprocess_files:
                with open(postprocess_status_file, encoding="utf-8") as f:
                    status_data = yaml.safe_load(f.read())

                if not status_data:
                    logger.warning(
                        f"Postprocess status - {postprocess_status_file} status_data is empty, skipping"
                    )
                    continue

                result = parse_postprocess_status(status_data)
                if not result:
                    logger.warning(
                        f"Postprocess status - {postprocess_status_file} parse_postprocess_status returned None/empty"
                    )
                    continue

                logger.info(
                    f"Postprocess status - {postprocess_status_file} parsed result successfully"
                )

                # Use shared unified get_file_link function
                get_file_link = _create_get_file_link(mlflow_run_url)

                # Generate notification text from the structured result
                logger.info(
                    f"Postprocess status - {postprocess_status_file} generating notification text..."
                )
                notification_text = format_postprocess_status_notification(result, get_file_link)
                if notification_text:
                    logger.info(
                        f"Postprocess status - {postprocess_status_file} notification text generated, adding to list"
                    )
                    step_log_links.append(notification_text)
                else:
                    logger.info(
                        f"Postprocess status - {postprocess_status_file} notification text is empty"
                    )

        except Exception as e:
            logger.exception(f"Failed to process postprocess status for {step_name}: {e}")

    return step_log_links


def _extract_artifact_links(status: ExportStatus) -> tuple[list[str], str | None]:
    """Extract artifact links and MLflow URL from status."""
    artifact_links = []
    mlflow_run_url = None

    caliper_export = status.caliper_artifacts_export
    if not caliper_export or not caliper_export.backends:
        return artifact_links, mlflow_run_url

    for backend_name, backend_result in caliper_export.backends.items():
        if not isinstance(backend_result, BackendResult):
            continue
        # Use typed access for BackendResult objects

        if backend_result.run_url:
            mlflow_run_url = backend_result.run_url
            artifact_links.append(f"* [{backend_name} results]({mlflow_run_url})")

        # Add child runs if they exist
        if not backend_result.child_runs:
            continue

        for child_run in backend_result.child_runs:
            run_name = child_run.get("run_name")
            run_url = child_run.get("run_url")
            if run_name and run_url:
                artifact_links.append(f"  * nested run [{run_name}]({run_url})")

    return artifact_links, mlflow_run_url


def _create_get_file_link(mlflow_run_url: str | None) -> callable:
    """Create a unified get_file_link function that always expects absolute paths.

    Args:
        mlflow_run_url: Base MLflow run URL, or None for fallback mode

    Returns:
        get_file_link function that takes absolute Path and returns URL
    """

    def get_file_link(file_path: Path | str, text: str = None) -> str:
        if not file_path:
            return "NO_FILE_RECEIVED"

        # Convert to Path object if it's a string, but warn that Path objects are preferred
        if isinstance(file_path, str):
            logger.warning(f"get_file_link received string instead of Path object: {file_path}")
            file_path = Path(file_path)

        return _create_mlflow_file_link(text or file_path.name, mlflow_run_url, file_path)

    return get_file_link


def _create_mlflow_file_link(text: str, mlflow_run_url: str, file_path: Path) -> str:
    """Create MLflow link for a specific file.

    Args:
        text: Text to display in the link
        mlflow_run_url: Base MLflow run URL
        file_path: Absolute path to file

    Returns:
        Markdown link format `[text](url)` if successful, `text (**error description**)` if not
    """
    if not mlflow_run_url:
        return text

    # Check file existence - still create link but with warning if file doesn't exist locally
    file_exists_locally = file_path.exists()
    if not file_exists_locally:
        logger.warning(f"File not found locally but creating MLflow link anyway: {file_path}")
        # We'll still create the link since the file might exist in MLflow

    if not file_path.is_absolute():
        return f"{text} (**path not absolute**)"

    try:
        # Convert absolute path to relative path for MLflow URL
        artifacts_base_dir = Path(
            os.environ.get("BASE_ARTIFACT_DIR") or env.BASE_ARTIFACT_DIR
        ).parent
        try:
            relative_path = file_path.relative_to(artifacts_base_dir)
        except ValueError:
            # File not under artifacts base, use the full path
            relative_path = file_path

        # Create the MLflow URL
        url = _create_mlflow_url(mlflow_run_url, relative_path)
        if url is None:
            return f"{text} (**URL creation failed**)"

        # Add indicator if file doesn't exist locally but we're still linking to MLflow
        if not file_exists_locally:
            return f"[{text}]({url}) ⚠️"
        else:
            return f"[{text}]({url})"
    except Exception as e:
        logger.error(f"Failed to create MLflow link for {file_path}: {e}")
        return f"{text} (**link creation failed**)"


def _create_mlflow_url(mlflow_run_url: str, file_path: Path) -> str | None:
    """Create MLflow URL for artifacts.

    Args:
        mlflow_run_url: Base MLflow run URL
        file_path: Relative path within artifacts (e.g., Path("01__test/run.log") or Path("data.csv"))

    Returns:
        Full MLflow URL to the artifact, or None/fallback URL if format is unexpected
    """
    from urllib.parse import urlparse

    if not mlflow_run_url:
        return f"BASE_URL_MISSING/{file_path}"

    try:
        if "/artifacts" not in mlflow_run_url:
            logger.error(f"Unexpected MLflow URL format: {mlflow_run_url}")
            return None

        # Convert file_path to string with forward slashes
        file_path_str = str(file_path).replace("\\", "/")

        # Parse the URL properly
        from urllib.parse import parse_qs

        parsed = urlparse(mlflow_run_url)

        # Handle case where there's no hash fragment - normalize to hash-based format
        if not parsed.fragment:
            raise ValueError(f"Hash fragment missing in the MLFLow URL {mlflow_run_url} ...")

        # Parse the fragment to extract any query parameters within it
        fragment = parsed.fragment
        fragment_path = fragment
        workspace_param = None

        # Check if there are query parameters in the fragment (like ?workspace=forge-sandbox)
        if "?" in fragment:
            fragment_path, fragment_query = fragment.split("?", 1)
            fragment_params = parse_qs(fragment_query)

            # Extract workspace parameter if present
            if "workspace" in fragment_params:
                workspace_param = fragment_params["workspace"][0]

        # Build base URL
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        # Add path if present
        if parsed.path and parsed.path != "/":
            base_url += parsed.path

        # Combine query parameters: main URL query + workspace from fragment
        query_parts = []
        if parsed.query:
            query_parts.append(parsed.query)
        if workspace_param:
            query_parts.append(f"workspace={workspace_param}")

        # Clean fragment path (remove trailing slash)
        fragment_path = fragment_path.rstrip("/")

        url = f"{base_url}#{fragment_path}/{file_path_str}"

        if query_parts:
            url += f"?{'&'.join(query_parts)}"

        return url

    except Exception as e:
        logger.error(f"Failed to construct MLflow URL for {file_path}: {e}")
        return None


def _process_notification_files(step_dir: Path) -> list[str]:
    """Process notification files from step directory."""
    notifications_dir = step_dir / CI_METADATA_DIRNAME / "notifications"
    if not (notifications_dir.exists() and notifications_dir.is_dir()):
        return []

    notifications_from_files = []
    for notification_file in sorted(notifications_dir.glob("*.txt")):
        with open(notification_file, encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            continue

        subtitle = notification_file.stem.replace("__", " ").replace("_", " ").title()
        subtitle = re.sub(r"^\d+\s+", "", subtitle)
        notifications_from_files.append(f"##### {subtitle}")

        for line in content.splitlines():
            notifications_from_files.append(f"> {line}")

    return notifications_from_files


def _extract_postprocess_status_info(artifact_dir: Path, get_file_link=None) -> list[str]:
    """Extract post-processing status information from POSTPROCESS_STATUS_FILENAME files.

    Returns:
        List of formatted strings with postprocess step status (success only, no details)
    """
    postprocess_info_lines = []

    # Search for POSTPROCESS_STATUS_FILENAME files recursively
    postprocess_files = list(artifact_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"))

    if not postprocess_files:
        return []

    for postprocess_file in postprocess_files:
        try:
            with open(postprocess_file, encoding="utf-8") as f:
                postprocess_data = yaml.safe_load(f) or {}

            # Extract directory relative to artifact_dir
            relative_dir = postprocess_file.parent.relative_to(artifact_dir)
            if relative_dir.name == "status_files":
                relative_dir = relative_dir.parent
            dir_name = str(relative_dir) if relative_dir != Path(".") else "root"

            base_directory = postprocess_data.get("base_directory", "")

            # Extract overall status
            overall_success = postprocess_data.get("success", False)
            final_status = postprocess_data.get("final_status", "unknown")

            # Extract individual step statuses
            steps = postprocess_data.get("steps", [])
            step_statuses = []

            for step_dict in steps:
                for step_name, step_data in step_dict.items():
                    if isinstance(step_data, dict):
                        status = step_data.get("status", "unknown")
                        status_emoji = (
                            "✔️" if status == "success" else "❌" if status == "failed" else "⚪"
                        )
                        log_file = step_data.get("log_file")
                        if log_file and get_file_link:
                            log_path = Path(base_directory) / log_file
                            step_label = get_file_link(log_path, text=step_name)
                        else:
                            step_label = step_name
                        step_statuses.append(f"{status_emoji} {step_label}")

            # Format overall line
            overall_emoji = "✅" if overall_success else "❌"
            if step_statuses:
                steps_str = " " + " | ".join(step_statuses)
            else:
                steps_str = f" {final_status}"

            postprocess_info_lines.append(f"* {overall_emoji} **{dir_name}**: {steps_str}")

        except Exception as e:
            postprocess_info_lines.append(f"**{postprocess_file.name}**: Error reading file - {e}")

    return postprocess_info_lines


def _process_step_details(step_dir: Path, mlflow_run_url: str | None = None) -> list[str]:
    """Process test labels, caliper metadata, and postprocess status for a single step directory."""
    step_details = []

    get_file_link = _create_get_file_link(mlflow_run_url)

    # Extract caliper metadata for this specific step
    try:
        metadata_info = _format_caliper_metadata_info_for_step(get_file_link, step_dir)
        step_details.extend(metadata_info)
    except Exception as e:
        logger.warning(f"Failed to extract caliper metadata for step {step_dir.name}: {e}")

    # Extract censoring report for this specific step
    try:
        censoring_info = _format_censoring_report_info_for_step(step_dir, get_file_link)
        step_details.extend(censoring_info)
    except Exception as e:
        logger.warning(f"Failed to extract censoring report for step {step_dir.name}: {e}")

    # Extract postprocess status for this specific step
    try:
        postprocess_info = _extract_postprocess_status_info(step_dir, get_file_link)
        step_details.extend(postprocess_info)
    except Exception as e:
        logger.warning(f"Failed to extract postprocess status for step {step_dir.name}: {e}")

    return step_details


def _check_job_shutdown_status(artifact_dir: Path) -> dict[str, Any] | None:
    """Check if the job has been aborted via spec.shutdown field."""
    try:
        metadata_dir = ci_lib.get_ci_metadata_dir(artifact_dir, any_level=True)
        fournos_fjob_path = metadata_dir / "fournos_fjob.yaml"
        logger.info(f"Checking job shutdown status from {fournos_fjob_path}")
        if not fournos_fjob_path.exists():
            logger.info(f"fournos_fjob.yaml not found at {fournos_fjob_path}")
            return None

        with open(fournos_fjob_path, encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f)

        spec = fjob_data.get("spec", {})
        shutdown_value = spec.get("shutdown")
        logger.info(f"spec keys: {list(spec.keys())}, shutdown_value: {shutdown_value!r}")
        if shutdown_value:
            return {
                "shutdown_detected": True,
                "shutdown_value": shutdown_value,
                "is_aborted": shutdown_value.lower() == "stop",
            }

        return {"shutdown_detected": False, "shutdown_value": None, "is_aborted": False}
    except Exception as e:
        logger.warning(f"Failed to check job shutdown status: {e}")
        return None


def _read_step_exit_status(
    step_dir: Path, current_step_name: str | None = None
) -> tuple[str, StepStatus]:
    """Read exit status from step directory and return emoji and status enum."""

    try:
        exit_status_file = step_dir / CI_METADATA_DIRNAME / "exit_status.yaml"
        if not exit_status_file.exists():
            # Check if this is the current ongoing step
            if current_step_name and step_dir.name == current_step_name:
                return "🔄", StepStatus.ONGOING  # Ongoing step
            return "❓", StepStatus.UNKNOWN  # Unknown status if file doesn't exist

        with open(exit_status_file, encoding="utf-8") as f:
            exit_data = yaml.safe_load(f)

        return_code = exit_data.get("return_code")
        if return_code is None or return_code == 0:
            return "✅", StepStatus.SUCCESS
        else:
            return "❌", StepStatus.FAILURE
    except Exception as e:
        logger.warning(f"Failed to read exit status from {step_dir}: {e}")
        # Check if this is the current ongoing step even on error
        if current_step_name and step_dir.name == current_step_name:
            return "🔄", StepStatus.ONGOING  # Ongoing step
        return "❓", StepStatus.UNKNOWN  # Unknown status on error


def _check_postprocess_warnings(step_dir: Path) -> StepStatus:
    """Check for warning status in postprocess status file."""

    status = StepStatus.SUCCESS  # No postprocess warning/error, assume no warnings
    for status_file in step_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"):
        try:
            with open(status_file, encoding="utf-8") as f:
                status_data = yaml.safe_load(f)
        except Exception as e:
            logging.error(f"Failed to read {status_file} as yaml: {e}")
            status = StepStatus.WARNING
            continue

        if not status_data:
            continue

        # Check top-level success field for warning value
        success_value = status_data.get("success")
        if success_value == "warning":
            logging.warning(
                f"Post-process warning detected in {status_file}, setting the WARNING flag"
            )
            status = StepStatus.WARNING

        if success_value in ("failure", "error"):
            logging.error(f"Post-process {success_value} detected, raising the FAILURE flag")
            return StepStatus.FAILURE

    return status


def _get_overall_status_from_steps(artifact_dir: Path) -> str:
    """Check all step exit statuses and return overall status emoji."""

    try:
        current_step_name = Path(env.BASE_ARTIFACT_DIR).name

        step_statuses = []

        for step_dir in sorted(artifact_dir.iterdir()):
            if not step_dir.is_dir():
                continue
            if step_dir.name.startswith("."):
                continue

            # Only check directories that have run.log (actual steps)
            run_log = step_dir / "run.log"
            if not run_log.exists():
                continue

            _emoji, status = _read_step_exit_status(step_dir, current_step_name)

            step_statuses.append(status)

            # Check for postprocess warnings in this step (always check, regardless of exit status)
            postprocess_status = _check_postprocess_warnings(step_dir)
            step_statuses.append(postprocess_status)

        # Priority: failure > ongoing > warning > unknown > success
        if StepStatus.FAILURE in step_statuses:
            return "🔴"  # Any failure = red
        elif StepStatus.WARNING in step_statuses:
            return "🟠"  # Warning = orange
        elif StepStatus.UNKNOWN in step_statuses:
            return "🟠"  # Unknown = orange
        elif StepStatus.ONGOING in step_statuses:
            return "🟢"  # Ongoing --> success
        else:
            return "🟢"  # All successful = green

    except Exception as e:
        logger.exception(f"Failed to check step statuses: {e}")
        return "🔴"  # Error checking = red


def _get_overall_step_status(artifact_dir: Path) -> StepStatus:
    """Check all step exit statuses and return overall status as StepStatus enum."""

    try:
        current_step_name = Path(env.BASE_ARTIFACT_DIR).name

        step_statuses = []

        for step_dir in sorted(artifact_dir.iterdir()):
            if not step_dir.is_dir():
                continue
            if step_dir.name.startswith("."):
                continue

            # Only check directories that have run.log (actual steps)
            run_log = step_dir / "run.log"
            if not run_log.exists():
                continue

            _emoji, status = _read_step_exit_status(step_dir, current_step_name)
            step_statuses.append(status)

            # Check for postprocess warnings in this step (always check, regardless of exit status)
            postprocess_status = _check_postprocess_warnings(step_dir)
            step_statuses.append(postprocess_status)

        # Priority: failure > ongoing > warning > unknown > success
        if StepStatus.FAILURE in step_statuses:
            return StepStatus.FAILURE
        elif StepStatus.WARNING in step_statuses:
            return StepStatus.WARNING
        elif StepStatus.UNKNOWN in step_statuses:
            return StepStatus.UNKNOWN
        elif StepStatus.ONGOING in step_statuses:
            return StepStatus.ONGOING
        else:
            return StepStatus.SUCCESS

    except Exception as e:
        logger.exception(f"Failed to check step statuses: {e}")
        return StepStatus.FAILURE  # Error checking = failure


def _read_step_duration(step_dir: Path) -> str:
    """Read step duration from timing file."""

    timing_file = step_dir / CI_METADATA_DIRNAME / "test_duration.yaml"
    if not timing_file.exists():
        return ""

    try:
        with open(timing_file, encoding="utf-8") as f:
            timing_data = yaml.safe_load(f)

        formatted_duration = timing_data.get("duration", {}).get("formatted")
        return formatted_duration or ""
    except Exception as timing_error:
        logger.warning(f"Failed to read timing file {timing_file}: {timing_error}")
        return ""


def _read_total_duration(artifact_dir: Path | None) -> str:
    """Read and sum test durations from all step directories."""

    # Return empty duration if artifact_dir is None or unavailable
    if artifact_dir is None or not artifact_dir.exists():
        return ""

    total_seconds = 0
    step_count = 0

    # Scan all step directories for timing files
    for step_dir in artifact_dir.iterdir():
        if not step_dir.is_dir():
            continue

        timing_file = step_dir / CI_METADATA_DIRNAME / "test_duration.yaml"
        if not timing_file.exists():
            continue

        try:
            with open(timing_file, encoding="utf-8") as f:
                timing_data = yaml.safe_load(f)

            # Get raw duration in seconds for summing
            duration_seconds = timing_data.get("duration", {}).get("seconds")
            if duration_seconds is not None:
                total_seconds += duration_seconds
                step_count += 1

        except Exception as timing_error:
            logger.warning(f"Failed to read timing file {timing_file}: {timing_error}")
            continue

    if step_count == 0:
        return ""

    # Format total duration (similar to how individual step durations are formatted)
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)

    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"


def _process_step_status(artifact_dir: Path, mlflow_run_url: str) -> list[str]:
    """Process step logs from parent directory."""

    if not mlflow_run_url:
        logging.warning("mlflow_run_url not set. Will generate dummy links.")

    step_status = []

    current_step_name = Path(env.BASE_ARTIFACT_DIR).name

    for step_dir in sorted(artifact_dir.iterdir()):
        if not step_dir.is_dir():
            continue
        if step_dir.name.startswith("."):
            continue

        run_log = step_dir / "run.log"
        if not run_log.exists():
            continue

        step_name = step_dir.name.replace("__", " ").replace("_", " ").title()
        mlflow_log_link = _create_mlflow_url(step_name, mlflow_run_url, run_log)

        duration_str = _read_step_duration(step_dir)
        exit_status_emoji, exit_status = _read_step_exit_status(step_dir, current_step_name)

        step_status.append("")
        if duration_str:
            step_status.append(f"#### {exit_status_emoji} {mlflow_log_link} `{duration_str}`")
        else:
            step_status.append(f"#### {exit_status_emoji} {mlflow_log_link}")

        step_status.extend(_process_notification_files(step_dir))

        step_details = _process_step_details(step_dir, mlflow_run_url)

        # Add test execution info for this step
        if step_details["test_metadata_info"]:
            # Add Test Execution Overview if we have any step info
            step_status.append("")
            step_status.append("**Test Execution Overview**")

            step_status.extend([f"* {info}" for info in step_details["test_metadata_info"]])

        # Add postprocess info for this step
        if step_details["postprocess_info"]:
            step_status.extend([f"* {info}" for info in step_details["postprocess_info"]])

    return step_status


def _extract_duration_from_status(status: ExportStatus) -> str:
    """Extract duration from status object."""
    # Look for duration in status
    duration = status.duration
    if duration:
        return f" after {duration}"
    return ""


def _search_caliper_metadata_files(step_dir: Path) -> list[Path]:
    """Search for caliper metadata files in the step directory."""

    if not step_dir.exists():
        return []

    metadata_files = []
    # Search for METADATA_FILE (__caliper_test_metadata__.yaml) recursively
    for metadata_file in step_dir.rglob(METADATA_FILE):
        metadata_files.append(metadata_file)

    return metadata_files


def _format_censoring_report_info_for_step(step_dir: Path, get_file_link: Any) -> list[str]:
    """Format censoring report information for integration within step details."""
    censoring_report_path = step_dir / "censoring_report.yaml"

    if not censoring_report_path.exists():
        return []

    try:
        with open(censoring_report_path, encoding="utf-8") as f:
            report_data = yaml.safe_load(f)

        if not report_data:
            return []

        censoring_lines = []

        # Get censoring statistics
        total_files = report_data.get("total_files", 0)
        clean_files = report_data.get("clean_files", 0)
        safe_censored_files = report_data.get("safe_censored_files", 0)
        censored_files = report_data.get("censored_files", 0)  # Unexpected censoring

        # Create link to censoring report file
        try:
            censoring_report_link = get_file_link(censoring_report_path, text="Censoring Report")
            censoring_lines.append(f"* 🔒 {censoring_report_link}")
        except Exception as e:
            logger.warning(
                f"Failed to create link for censoring report {censoring_report_path}: {e}"
            )
            censoring_lines.append(f"* 🔒 Censoring Report **link generation failed** `{e}`")

        censoring_lines.append(f"    * 📊 {total_files} files scanned")

        # Show breakdown of file types
        if clean_files > 0:
            censoring_lines.append(f"    * ✅ Clean files: {clean_files}")

        if safe_censored_files > 0:
            censoring_lines.append(f"    * 🔐 Safe replacements: {safe_censored_files}")

        if censored_files > 0:
            censoring_lines.append(f"    * ⚠️ Unexpected censoring: {censored_files}")

            # Show details for unexpected censoring if available
            censored_by_reason = report_data.get("censored_by_reason", {})
            if censored_by_reason:
                for reason, files in censored_by_reason.items():
                    file_count = len(files)
                    # Truncate reason if too long
                    display_reason = reason[:50] + "..." if len(reason) > 50 else reason
                    censoring_lines.append(f"    * {display_reason}: {file_count} file(s)")

        return censoring_lines

    except Exception as e:
        logger.warning(f"Failed to process censoring report {censoring_report_path}: {e}")
        return [f"* 🔒 Censoring Report: Error reading report - `{e}`"]


def _format_caliper_metadata_info_for_step(get_file_link: Any, step_dir: Path) -> list[str]:
    """Format caliper metadata information for integration within step details."""

    metadata_files = _search_caliper_metadata_files(step_dir)
    metadata_lines = []

    for metadata_file in metadata_files:
        try:
            # Load and parse metadata
            with open(metadata_file, encoding="utf-8") as f:
                metadata_dict = yaml.safe_load(f)

            metadata = CaliperTestMetadata.from_dict(metadata_dict)

            # Get relative path from base directory
            try:
                relative_path = metadata_file.parent.relative_to(step_dir)
                display_path = str(relative_path) if str(relative_path) != "." else "root"
            except ValueError:
                # If relative_to fails, use the full path
                display_path = str(metadata_file.parent)

            # Extract information
            labels = metadata.labels or {}
            kpi_labels = metadata.kpi_labels or {}
            timing = metadata.timing

            # Format path with link to metadata file
            metadata_file_link = get_file_link(metadata_file, text=f"`{display_path}`")

            # Add completion status emoji to the test directory line
            completion_emoji = "📊"
            if metadata.completion:
                if metadata.completion.success:
                    pass  # no change
                elif metadata.completion.success is False:
                    completion_emoji = "❌"
                else:
                    completion_emoji = "❓"

            path_info = f"* {completion_emoji} **Test directory**: {metadata_file_link}"
            metadata_lines.append(path_info)

            # Add completion message as a separate line
            if metadata.completion and metadata.completion.message:
                metadata_lines.append(f"  * `{metadata.completion.message}`")

            # Format labels
            if labels:
                label_items = [f"`{k}={v}`" for k, v in labels.items()]
                metadata_lines.append(f"  * {', '.join(label_items)}")

            # Format KPI labels
            if kpi_labels:
                kpi_label_items = [f"`{k}={v}`" for k, v in kpi_labels.items()]
                metadata_lines.append(f"  * KPI extra labels: {', '.join(kpi_label_items)}")

            # Format timing information - show durations instead of timestamps
            duration_parts = []

            if timing:
                for phase_name, timing_entry in timing.phases.items():
                    if timing_entry.end:  # Only show duration if we have both start and end
                        duration = _calculate_duration(timing_entry.start, timing_entry.end)
                        duration_parts.append(f"`{phase_name}: {duration}`")

            if duration_parts:
                metadata_lines.append(f"  * Duration {', '.join(duration_parts)}")

        except Exception as e:
            logger.warning(f"Failed to process caliper metadata file {metadata_file}: {e}")
            relative_path = metadata_file.parent.relative_to(step_dir)
            metadata_lines.append(
                f"* 📊 Test directory: `{relative_path}` - Error reading metadata: {e}"
            )

    return metadata_lines
