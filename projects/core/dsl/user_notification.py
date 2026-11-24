"""
User notification messages from toolbox scripts.

Leveled messages (info / warning / error) written as artifact files under
ARTIFACT_DIR/_user_notification/, surfaced by the export notification
(projects/core/library/export_notifications.py).

This is the notification channel reachable from the toolbox layer: pure file
writes, no state, no aggregation, no imports above the DSL.
"""

import logging

import projects.core.library.env as env

logger = logging.getLogger(__name__)

LEVELS = ("info", "warning", "error")

DIRECTORY_NAME = "_user_notification"


def _write(level: str, name: str, message: str) -> str | None:
    """
    Write a leveled user notification file with the next available index.

    The index is global to the _user_notification directory, so that the
    export notification renders the messages in emission order.

    Args:
        level: Message level (info, warning, error)
        name: Base name for the notification file (without extension)
        message: Content to write to the file

    Returns:
        Path to the created notification file, or None if creation failed
    """
    if level not in LEVELS:
        raise ValueError(f"Invalid user notification level: {level!r} (expected one of {LEVELS})")
    if not name:
        raise ValueError("A user notification requires a non-empty name")

    notifications_dir = env.ARTIFACT_DIR / DIRECTORY_NAME
    notifications_dir.mkdir(parents=True, exist_ok=True)

    # Find the next available index across the whole directory
    existing_indices = [
        int(path.name.split("__", 1)[0])
        for path in notifications_dir.glob("*.txt")
        if path.name.split("__", 1)[0].isdigit()
    ]
    index = max(existing_indices) + 1 if existing_indices else 0

    filename = f"{index:03d}__{level}__{name}.txt"
    file_path = notifications_dir / filename

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(message)

        getattr(logger, level)(f"User notification ({level}) written to {file_path}")
        return str(file_path)

    except Exception as e:
        logger.error(f"Failed to create user notification file {file_path}: {e}")
        return None


def info(name: str, message: str) -> str | None:
    """
    Record an informational message in the artifacts.

    Rendered in the export notification like an orchestration notification file.
    """
    return _write("info", name, message)


def warning(name: str, message: str) -> str | None:
    """
    Record a warning message in the artifacts.

    Surfaced in the export notification and flips the step overall status
    to WARNING (orange) — the run itself stays successful.
    """
    return _write("warning", name, message)


def error(name: str, message: str) -> str | None:
    """
    Record an error message in the artifacts.

    Surfaced in the export notification, but non-fatal: does not affect the
    exit status or the success of the run. Fatal errors should be raised instead.
    """
    return _write("error", name, message)
