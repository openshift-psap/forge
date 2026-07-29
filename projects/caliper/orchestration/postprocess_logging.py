"""
Command logging and execution utilities for Caliper postprocessing.

Handles formatting commands for logs, writing log headers/footers, and executing
commands with proper logging and status handling.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _format_command_for_log(command: list[str]) -> str:
    """Format command for log file display with only --arguments on new lines.

    Args:
        command: Command arguments list

    Returns:
        Formatted command string for log file
    """
    if not command:
        return ""

    # Start with base command parts until we hit options
    lines = []
    base_parts = []
    i = 0

    # Collect all non-flag arguments into base_parts
    while i < len(command) and not command[i].startswith("--"):
        base_parts.append(command[i])
        i += 1

    if base_parts:
        lines.append(" ".join(base_parts))

    # Handle --flag arguments on separate lines
    while i < len(command):
        if command[i].startswith("--"):
            if i + 1 < len(command) and not command[i + 1].startswith("--"):
                # Flag with value
                lines.append(f"{command[i]} {command[i + 1]}")
                i += 2
            else:
                # Boolean flag
                lines.append(command[i])
                i += 1
        else:
            # Standalone argument
            lines.append(command[i])
            i += 1

    return " \\\n  ".join(lines)


def _write_step_header_to_log_file(log_file: Path, command: list[str], step_name: str) -> None:
    """Write step header to log file.

    Args:
        log_file: Path to log file
        command: Command that will be executed
        step_name: Name of the step (e.g., "CALIPER PARSE")
    """
    formatted_command = _format_command_for_log(command)
    header = f"""{"=" * 60}
🚀 STARTING {step_name} (fork/exec)
📄 Command:
  {formatted_command}
{"=" * 60}

"""
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(header)


def _write_step_footer_to_log_file(
    log_file: Path, result: subprocess.CompletedProcess, step_name: str
) -> None:
    """Write step footer to log file.

    Args:
        log_file: Path to log file
        result: Subprocess result
        step_name: Name of the step (e.g., "CALIPER PARSE")
    """
    footer = f"""

{"=" * 60}
🏁 {step_name} COMPLETED
📊 Exit code: {result.returncode}
📄 Log file: {log_file}
{"=" * 60}
"""
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(footer)


def _handle_caliper_output_and_completion_with_header(
    result: subprocess.CompletedProcess,
    log_file: Path,
    status_file: Path,
    step_name: str,
) -> dict:
    """Handle Caliper output writing, status parsing, and log completion banner.

    This version appends to the log file instead of overwriting it, since we've
    already written a header.

    Args:
        result: Subprocess result from caliper execution
        log_file: Path to write the step log file
        status_file: Path to the status file to read and parse
        step_name: Name of the Caliper step (e.g., "PARSE", "VISUALIZE")

    Returns:
        Parsed status data dictionary
    """
    import yaml

    # Append output to log file (don't overwrite since we've written header)
    with open(log_file, "a", encoding="utf-8") as log_f:
        if result.stdout:
            log_f.write(result.stdout)

    # Also display output in main orchestration logs
    if result.stdout:
        prefix = step_name.upper()
        for line in result.stdout.splitlines():
            logger.info("%s: %s", prefix, line)

    # Log banner indicating Caliper execution completed
    logger.info("=" * 60)
    logger.info("🏁 CALIPER %s COMPLETED", step_name.upper())
    logger.info("📊 Exit code: %s", result.returncode)
    logger.info("📄 Log file: %s", log_file)

    # Read and parse status file
    try:
        with open(status_file, encoding="utf-8") as f:
            status_data = yaml.safe_load(f)
        # Handle case where YAML file is empty or invalid
        if status_data is None:
            status_data = {
                "success": False,
                "error": "Status file is empty or contains no valid YAML data",
            }
        # Normalize non-mapping payloads (lists, strings, integers) into failure dict
        elif not isinstance(status_data, dict):
            status_data = {
                "success": False,
                "error": f"Status file contains non-mapping data: {type(status_data).__name__}",
                "raw_data": status_data,
            }
    except Exception as e:
        status_data = {"success": False, "error": f"Failed to read status file: {e}"}

    # Log status file content for visibility
    logger.info("📄 Status file content:")
    if isinstance(status_data, dict):
        for key, value in status_data.items():
            logger.info("   %s: %s", key, value)
    else:
        logger.info("   %s", status_data)

    logger.info("=" * 60)

    return status_data
