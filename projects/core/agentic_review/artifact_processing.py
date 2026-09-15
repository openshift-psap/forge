"""
Generic Artifact Processing for FORGE Agentic Processing

This module contains functions for reading, parsing, and processing artifact files
from FORGE test executions that can be used across different agentic workflows.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_failures_file(failures_file: Path) -> str:
    """
    Read the FAILURES file content for LLM processing

    Args:
        failures_file: Path to the FAILURES file

    Returns:
        Raw content of the FAILURES file
    """
    if not failures_file.exists():
        logger.warning(f"FAILURES file not found: {failures_file}")
        return ""

    try:
        with open(failures_file) as f:
            content = f.read().strip()
            logger.info(f"Read FAILURES file: {len(content)} characters")
            return content
    except Exception as e:
        logger.error(f"Failed to read FAILURES file: {e}")
        return ""


def find_execution_logs(base_artifact_dir: Path) -> list[Path]:
    """
    Find all execution log files (task.log) in the artifact directory

    Args:
        base_artifact_dir: Base artifact directory

    Returns:
        List of paths to execution log files
    """
    execution_logs = []

    for log_file in base_artifact_dir.rglob("task.log"):
        if log_file.exists():
            execution_logs.append(log_file)
            logger.debug(f"Found execution log: {log_file}")

    logger.info(f"Found {len(execution_logs)} execution log files")
    return execution_logs


def read_run_log(base_artifact_dir: Path) -> str:
    """
    Read the run.log file for additional context

    Args:
        base_artifact_dir: Base artifact directory

    Returns:
        Content of run.log or empty string if not found
    """
    run_log_path = base_artifact_dir / "run.log"
    if not run_log_path.exists():
        logger.warning(f"run.log not found: {run_log_path}")
        return ""

    try:
        with open(run_log_path) as f:
            content = f.read()
            logger.info(f"Read run.log: {len(content)} characters")
            return content
    except Exception as e:
        logger.error(f"Failed to read run.log: {e}")
        return ""


def read_execution_log_errors(log_path: Path, max_lines: int = 200) -> str:
    """
    Read and extract relevant error information from execution log

    Args:
        log_path: Path to execution log file
        max_lines: Maximum number of lines to read from end of file

    Returns:
        Relevant error content from the log
    """
    try:
        with open(log_path) as f:
            lines = f.readlines()

        # Get last max_lines for recent errors
        recent_lines = lines[-max_lines:] if len(lines) > max_lines else lines

        # Look for error patterns
        error_indicators = [
            "ERROR",
            "FAILED",
            "FATAL",
            "failed:",
            "error:",
            "Error:",
            "==> TASK FAILED:",
        ]
        error_lines = []

        for i, line in enumerate(recent_lines):
            if any(indicator in line for indicator in error_indicators):
                # Include some context around error lines
                start_idx = max(0, i - 2)
                end_idx = min(len(recent_lines), i + 3)
                context = recent_lines[start_idx:end_idx]
                error_lines.extend(context)

        # If no specific errors found, return the last portion of the log
        if not error_lines:
            error_lines = recent_lines[-50:] if len(recent_lines) > 50 else recent_lines

        result = "".join(error_lines)
        logger.info(f"Extracted {len(error_lines)} error lines from {log_path}")
        return result

    except Exception as e:
        logger.error(f"Failed to read execution log {log_path}: {e}")
        return ""


def find_failure_files(base_artifact_dir: Path) -> list[Path]:
    """
    Find all FAILURE files in the artifact directory

    Args:
        base_artifact_dir: Base artifact directory to search

    Returns:
        List of paths to FAILURE files
    """
    failure_files = []

    for failure_file in base_artifact_dir.rglob("FAILURE.txt"):
        if not failure_file.exists():
            continue
        if failure_file.parent == base_artifact_dir:
            continue
        failure_files.append(failure_file)
        logger.debug(f"Found FAILURE file: {failure_file}")

    if not failure_files:
        base_failure = base_artifact_dir / "FAILURE.txt"
        if base_failure.exists():
            failure_files.append(base_failure)

    logger.info(f"Found {len(failure_files)} FAILURE files")
    return failure_files


def read_failure_and_log(failure_file: Path) -> dict:
    """
    Read a FAILURE file and its corresponding log file (task.log)

    Args:
        failure_file: Path to the FAILURE file

    Returns:
        Dictionary with failure content and log content
    """
    failure_dir = failure_file.parent

    # Check for task.log file
    task_log = failure_dir / "task.log"

    log_file = None
    log_type = None

    if task_log.exists():
        log_file = task_log
        log_type = "task"

    # Check for AGENT.md file for post-mortem analysis guidance
    agent_md_file = failure_dir / "AGENT.md"
    agent_md_content = ""
    if agent_md_file.exists():
        try:
            with open(agent_md_file) as f:
                agent_md_content = f.read().strip()
                logger.debug(f"Found AGENT.md file: {len(agent_md_content)} characters")
        except Exception as e:
            logger.warning(f"Failed to read AGENT.md file {agent_md_file}: {e}")
            agent_md_content = f"Error reading AGENT.md file: {e}"

    result = {
        "failure_file": str(failure_file),
        "failure_dir": str(failure_dir),
        "failure_content": "",
        "log_file": str(log_file) if log_file else "No log file found",
        "log_type": log_type,
        "log_content": "",  # Renamed from ansible_content
        "agent_md_file": str(agent_md_file) if agent_md_content else "No AGENT.md file found",
        "agent_md_content": agent_md_content,
    }

    # Read FAILURE file
    try:
        with open(failure_file) as f:
            result["failure_content"] = f.read().strip()
            logger.debug(f"Read FAILURE file: {len(result['failure_content'])} characters")
    except Exception as e:
        logger.warning(f"Failed to read FAILURE file {failure_file}: {e}")
        result["failure_content"] = f"Error reading FAILURE file: {e}"

    # Read corresponding log file
    if not (log_file and log_file.exists()):
        logger.warning(f"No log file found in {failure_dir} (checked: task.log)")
        result["log_content"] = "No log file (task.log) found in the same directory"

        return result

    try:
        with open(log_file) as f:
            content = f.read()

            # Process DSL task.log files - use content as-is but extract failure patterns
            result["log_content"] = content

            # Extract and highlight failed tasks for DSL logs
            failed_task_lines = []
            lines = content.splitlines()
            for i, line in enumerate(lines):
                if "==> TASK EXCEPTION:" in line:
                    failed_task_lines.append(f"Line {i + 1}: {line.strip()}")

            if failed_task_lines:
                result["failed_tasks_summary"] = "\n".join(failed_task_lines)
                logger.info(
                    f"Found {len(failed_task_lines)} '==> TASK FAILED:' patterns in {log_file}"
                )
            else:
                result["failed_tasks_summary"] = "No '==> TASK FAILED:' patterns found"

            logger.debug(f"Read {log_type} log: {len(result['log_content'])} characters")
            logger.info(f"Using {log_type} log file: {log_file}")
    except Exception as e:
        logger.warning(f"Failed to read {log_type} log {log_file}: {e}")
        result["log_content"] = f"Error reading {log_type} log: {e}"

    return result


def read_failure_and_ansible_log(failure_file: Path) -> dict:
    """Backward compatibility alias for read_failure_and_log"""
    result = read_failure_and_log(failure_file)
    # Add backward compatibility for old key name
    result["ansible_content"] = result.get("log_content", "")
    return result


def list_all_files_in_artifact_dir(base_artifact_dir: Path) -> list[str]:
    """
    List all files recursively in the entire artifact directory

    Args:
        base_artifact_dir: Base artifact directory to search recursively

    Returns:
        List of relative file paths from the base artifact directory
    """
    try:
        files = []
        for file_path in base_artifact_dir.rglob("*"):
            if file_path.is_file():
                # Get relative path from base artifact directory
                relative_path = file_path.relative_to(base_artifact_dir)
                files.append(str(relative_path))
        files.sort()
        logger.debug(f"Listed {len(files)} files recursively in {base_artifact_dir}")
        return files
    except Exception as e:
        logger.warning(f"Failed to list files in {base_artifact_dir}: {e}")
        return []
