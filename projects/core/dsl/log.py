"""
Logging utilities for the DSL framework
"""

import inspect
import logging
import time
from datetime import datetime
from pathlib import Path

import projects.core.library.env as env

LINE_WIDTH = 80


def setup_clean_logger(name: str):
    """Set up logger that shows only the message without prefix"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(message)s"))

        logger.addHandler(console_handler)

    logger.propagate = False
    return logger


logger = setup_clean_logger("DSL")


def log_task_header(
    task_name: str,
    task_doc: str,
    rel_filename: str,
    line_no: int,
    artifact_dirname_suffix: str = None,
    start_time: float = None,
):
    """Log the verbose task header with tildes"""
    logger.info("")
    logger.info("~" * LINE_WIDTH)

    # Build the file/line info with optional suffix
    file_line_info = f"~~ {rel_filename}:{line_no}"

    # Add suffix if available, with underscores stripped
    if artifact_dirname_suffix:
        # Strip underscores and add to display
        display_suffix = artifact_dirname_suffix.strip("_")
        file_line_info += f" [{display_suffix}]"

    logger.info(file_line_info)

    # Build task line with timestamp and elapsed time if start_time provided
    current_time = time.time()
    timestamp = datetime.fromtimestamp(current_time).strftime("%Y-%m-%d %H:%M:%S")
    if start_time is not None:
        elapsed_time = current_time - start_time
        elapsed_mins, elapsed_secs = divmod(elapsed_time, 60)
        logger.info(f"~~ TASK: {task_name} : {(task_doc or 'No description').strip()}")
        logger.info(f"~~ {timestamp} ({elapsed_time:.0f}s {elapsed_mins:.0f}m {elapsed_secs:.0f}s)")
        logger.info("~" * LINE_WIDTH)
        logger.info("")
        return
    else:
        logger.info(f"~~ TASK: {task_name} : {(task_doc or 'No description').strip()}")
        logger.info(f"~~ {timestamp}")
    logger.info("~" * LINE_WIDTH)
    logger.info("")


def log_execution_banner(function_args: dict = None, log_file: str = None):
    """Log the execution banner with function info and arguments"""
    frame = inspect.currentframe()
    caller_frame = frame.f_back.f_back
    filename = caller_frame.f_code.co_filename

    rel_filename = _get_forge_relative_path(filename)
    function_name = _get_toolbox_function_name(filename)

    logger.info("")
    logger.info("===============================================================================")
    logger.info(f"| FILE: {rel_filename}")
    logger.info(f"| COMMAND: {function_name}")

    if function_args:
        logger.info("| ARGUMENTS:")

        for key, value in function_args.items():
            if key == "function_args":
                continue
            if value is None:
                continue

            logger.info(f"|   {key}: {value}")

    logger.info(f"| ARTIFACT_DIR: {env.ARTIFACT_DIR}")
    logger.info(f"| LOG_FILE: {log_file}")
    logger.info("===============================================================================")
    logger.info("")


def log_completion_banner(function_args: dict = None, status: str = "SUCCESS"):
    """Log the completion banner with function info and completion status"""
    frame = inspect.currentframe()
    caller_frame = frame.f_back.f_back
    filename = caller_frame.f_code.co_filename

    rel_filename = _get_forge_relative_path(filename)
    function_name = _get_toolbox_function_name(filename)

    logger.info("")
    logger.info("===============================================================================")
    logger.info(f"| {rel_filename}")
    logger.info(f"| STATUS: {status}")
    logger.info(f"| COMMAND: {function_name}")
    logger.info(f"| ARTIFACTS: {env.ARTIFACT_DIR}")
    logger.info("===============================================================================")
    logger.info("")


def _get_forge_relative_path(filename):
    """Get file path relative to FORGE home directory (forge root)"""
    filename_path = Path(filename)

    return filename_path.relative_to(env.FORGE_HOME)


def _get_toolbox_function_name(filename):
    """Extract toolbox function name from file path (parent directory name)"""
    return Path(filename).parent.name
