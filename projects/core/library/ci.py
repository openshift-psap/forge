#!/usr/bin/env python3
"""
Shared CI utilities for FORGE projects

Provides common CI functionality including error handling, logging,
and tooling setup for consistent behavior across all projects.
"""

import dataclasses
import functools
import logging
import os
import sys
import traceback
from enum import StrEnum
from pathlib import Path

import click
import yaml

from projects.core.dsl import toolbox as dsl_toolbox
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.dsl.utils.k8s import oc
from projects.core.library import env

logger = logging.getLogger(__name__)

EXIT_STATUS_FILENAME = "exit_status.yaml"


class ExitCategory(StrEnum):
    SUCCESS = "success"
    TEST_FAILURE = "test_failure"
    INFRA_FAILURE = "infra_failure"
    SIGNAL_ABORT = "signal_abort"
    CONFIG_ERROR = "config_error"
    INTERNAL_ERROR = "internal_error"


class CIError(Exception):
    """Exception with an associated ExitCategory for structured exit status reporting."""

    def __init__(self, message: str, category: ExitCategory):
        super().__init__(message)
        self.category = category


@dataclasses.dataclass
class ExitRecord:
    return_code: int
    category: ExitCategory
    reason: str
    details: str = ""

    def to_dict(self) -> dict:
        d = {
            "return_code": self.return_code,
            "category": str(self.category),
            "reason": self.reason,
        }
        if self.details:
            d["details"] = self.details
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ExitRecord":
        return cls(
            return_code=data["return_code"],
            category=ExitCategory(data["category"]),
            reason=data.get("reason", ""),
            details=data.get("details", ""),
        )


@dataclasses.dataclass
class ExitStatus:
    records: list[ExitRecord] = dataclasses.field(default_factory=list)

    def add(self, return_code: int, category: ExitCategory, reason: str) -> None:
        first_line, _, remaining = reason.partition("\n")
        self.records.append(
            ExitRecord(
                return_code=return_code,
                category=category,
                reason=first_line,
                details=remaining.strip(),
            )
        )

    def save(self, metadata_dir: Path) -> None:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        exit_status_file = metadata_dir / EXIT_STATUS_FILENAME
        data = {"records": [r.to_dict() for r in self.records]}
        with open(exit_status_file, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Exit status saved: {exit_status_file}")

    @classmethod
    def load(cls, metadata_dir: Path) -> "ExitStatus":
        exit_status_file = metadata_dir / EXIT_STATUS_FILENAME
        if not exit_status_file.exists():
            return cls()

        with open(exit_status_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "records" not in data:
            return cls()

        records = [ExitRecord.from_dict(r) for r in data["records"]]
        return cls(records=records)

    @property
    def is_success(self) -> bool:
        return bool(self.records) and all(r.return_code == 0 for r in self.records)

    @property
    def primary(self) -> ExitRecord | None:
        return self.records[0] if self.records else None


def get_ci_metadata_dir_location():
    metadata_dir = env.BASE_ARTIFACT_DIR / "000__ci_metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    return metadata_dir


# CI metadata directory path
def get_ci_metadata_dir(base_ci_dir, any_level=False):
    """Get the CI metadata directory path.

    Args:
        base_ci_dir: Optional base directory. If not provided, uses env.BASE_ARTIFACT_DIR

    Returns:
        Path to the CI metadata directory

    Raises:
        ValueError: If both base_ci_dir and env.BASE_ARTIFACT_DIR are None
    """

    meta_dir = base_ci_dir / "000__ci_metadata"
    if meta_dir.exists():
        return meta_dir

    if not any_level:
        raise ValueError(f"Cannot determine CI metadata directory in {base_ci_dir}")

    rglob = list(base_ci_dir.rglob("000__ci_metadata"))
    if not rglob:
        raise ValueError(f"Cannot find CI metadata directory at any level of {base_ci_dir}")

    return rglob[0]


class HelpfulGroup(click.Group):
    """
    A Click group that automatically shows help when a command is not found.
    """

    def get_command(self, ctx, cmd_name):
        rv = click.Group.get_command(self, ctx, cmd_name)
        if rv is not None:
            return rv

        # Command not found - show help
        print(f"Error: No such command '{cmd_name}'.\n", file=sys.stderr)
        print(ctx.get_help(), file=sys.stderr)
        ctx.exit(2)


def handle_ci_exception(e: Exception) -> None:
    """
    Handle CI exceptions with comprehensive logging and failure file creation.

    Args:
        e: The exception that occurred
    """

    # Display error on screen and write to FAILURES file
    _display_error_summary(e)


def _display_error_summary(e: Exception) -> None:
    """Display a comprehensive error summary on screen and write to FAILURES file."""

    summary_lines = []
    if isinstance(e, TaskExecutionError):
        summary_lines += dsl_toolbox.get_task_execution_error(e)
    else:
        summary_lines.append(f"{e.__class__.__name__}: {e}")

    # mind that anything below a '---\n' will be cut in the notification
    summary_lines.append("")
    summary_lines.append("---")

    full_traceback = traceback.format_exc().splitlines()
    for line in full_traceback:
        summary_lines.append(f"   {line}")

    summary_lines.append("---")  # add the details marker after the stacktrace

    header = "\n".join(["", "=" * 80, "🚨 CI EXECUTION FAILED", "=" * 80, ""])
    logger.error(header)
    logger.error("--- 📍%s STACKTRACE ---", e.__class__.__name__)
    logger.error("--- 📍%s", e)
    logger.info("\n".join([""] + summary_lines + ["=" * 80]))

    # Attempt to write to FAILURES file if environment is initialized
    try:
        if env.ARTIFACT_DIR is not None:
            _write_error_summary_to_file(summary_lines)
        else:
            logger.warning(
                "Environment not fully initialized - error summary displayed above but not saved to file"
            )
    except Exception as write_error:
        logger.warning(f"Could not save error summary to file: {write_error}")


def _write_error_summary_to_file(summary_lines: list) -> None:
    """Write the error summary to FAILURE file."""
    failures_file = env.ARTIFACT_DIR / "FAILURE.txt"

    try:
        content = "\n".join(summary_lines)
        failures_file.write_text(content + "\n")
        logger.info(f"Error summary written to: {failures_file}")
    except Exception as write_error:
        logger.error(f"Failed to write error summary to file: {write_error}")


def _run_ci_function(command_func, args, kwargs):
    """Run a CI function with consistent error handling and result parsing.

    Returns:
        (exit_code, category, reason) tuple
    """
    try:
        result = command_func(*args, **kwargs)
        if result is None or result == 0:
            return 0, ExitCategory.SUCCESS, None
        elif isinstance(result, tuple) and len(result) == 3:
            return result
        else:
            logger.error(
                f"Unexpected return value from {command_func.__name__}: {result!r}. "
                "Expected None, 0, or (return_code, ExitCategory, reason)."
            )
            return 1, ExitCategory.INTERNAL_ERROR, f"Unexpected return value: {result!r}"
    except Exception as e:
        handle_ci_exception(e)
        category = e.category if isinstance(e, CIError) else ExitCategory.INTERNAL_ERROR
        return 1, category, str(e)


def safe_ci_entrypoint(command_func):
    """
    Decorator for CI phase entrypoints (prepare, test, cleanup, ...).

    Saves exit status to exit_status.yaml then calls sys.exit().
    """

    @functools.wraps(command_func)
    def wrapper(*args, **kwargs):
        exit_code, category, reason = _run_ci_function(command_func, args, kwargs)
        logger.info(
            f"[safe_ci_entrypoint] {command_func.__name__}: "
            f"exit_code={exit_code}, category={category}, reason={reason!r}"
        )
        save_exit_status(exit_code, category, reason)
        sys.exit(exit_code)

    return wrapper


def safe_ci_function(command_func):
    """
    Decorator for CI group commands (main).

    Saves exit status and calls sys.exit() on failure.
    Does NOT exit on success — lets Click proceed to subcommands.
    """

    @functools.wraps(command_func)
    def wrapper(*args, **kwargs):
        exit_code, category, reason = _run_ci_function(command_func, args, kwargs)
        if exit_code != 0:
            logger.info(
                f"[safe_ci_function] {command_func.__name__}: "
                f"exit_code={exit_code}, category={category}, reason={reason!r}"
            )
            save_exit_status(exit_code, category, reason)
            sys.exit(exit_code)

    return wrapper


def save_exit_status(return_code: int, category: ExitCategory, reason: str | None = None) -> None:
    if category != ExitCategory.SUCCESS and not reason:
        logger.warning(f"No reason supplied for category {category}", stack_info=True)
        reason = f"{category} (no reason provided)"

    try:
        metadata_dir = get_ci_metadata_dir_location()
        exit_status = ExitStatus.load(metadata_dir)
        exit_status.add(return_code, category, reason or "")
        exit_status.save(metadata_dir)
    except Exception as save_error:
        logger.warning(f"Failed to save exit status: {save_error}")


def add_notification_file(
    name: str, message: str, start_index: int = 0, base_ci_dir=None
) -> str | None:
    """
    Add a notification file in CI metadata notifications directory with the next available index.

    Args:
        name: Base name for the notification file (without extension)
        message: Content to write to the file
        start_index: Starting index to search for available slot
        base_ci_dir: Optional base directory. If not provided, uses env.BASE_ARTIFACT_DIR

    Returns:
        Path to the created notification file, or None if creation failed
    """
    metadata_dir = get_ci_metadata_dir_location()
    notifications_dir = metadata_dir / "notifications"
    notifications_dir.mkdir(parents=True, exist_ok=True)

    # Find the next available index
    index = start_index
    while True:
        filename = f"{index:03d}__{name}.txt"
        file_path = notifications_dir / filename

        if not file_path.exists():
            break

        index += 1

    # Write the notification file
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(message)

        logger.info(f"Created notification file: {file_path}")
        logger.info(f"{message}")
        return str(file_path)

    except Exception as e:
        logger.error(f"Failed to create notification file {file_path}: {e}")
        return None


def ensure_kubeconfig_works():
    if os.environ.get("FORGE_SKIP_CLUSTER_CHECK"):
        logger.info("FORGE_SKIP_CLUSTER_CHECK is set, skipping kubeconfig check")
        return

    kubeconfig = os.environ.get("KUBECONFIG")
    if not kubeconfig:
        raise CIError("KUBECONFIG environment variable is not set", ExitCategory.CONFIG_ERROR)

    logger.info(f"KUBECONFIG is set to {kubeconfig}")

    result = oc("whoami", check=False)
    if result.returncode != 0:
        logging.error(f"Cluster is not reachable (KUBECONFIG={kubeconfig}): oc whoami failed")
        raise CIError(
            "Cluster is not reachable: oc whoami failed",
            ExitCategory.INFRA_FAILURE,
        )
    logger.info(f"Cluster is reachable at {result.stdout.strip()}")
