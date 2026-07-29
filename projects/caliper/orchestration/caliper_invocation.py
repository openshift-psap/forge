"""
Caliper CLI command invocation functions.

This module contains functions that build and execute caliper CLI commands
using subprocess execution with proper logging and status handling.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from projects.caliper.orchestration.cli_builder import (
    build_ai_eval_export_command,
    build_analyse_kpis_command,
    build_kpi_csv_export_command,
    build_kpi_generate_command,
    build_parse_command,
    build_s3_export_command,
    build_s3_import_command,
    build_visualize_command,
)
from projects.caliper.orchestration.postprocess_config import (
    CaliperOrchestrationPostprocessConfig,
)
from projects.core.library import env

logger = logging.getLogger(__name__)


def _make_path_relative_to_base(file_path: str | Path, base_dir: Path) -> str:
    """Convert absolute path to relative path from base directory.

    Args:
        file_path: Absolute or relative file path
        base_dir: Base directory to make path relative to

    Returns:
        Relative path as string
    """
    try:
        path_obj = Path(file_path)
        if path_obj.is_absolute() and base_dir:
            base_obj = Path(base_dir)
            if base_obj.is_absolute():
                try:
                    relative = path_obj.relative_to(base_obj)
                    return str(relative)
                except ValueError:
                    # Path is not relative to base_dir, return as-is
                    return str(file_path)
        return str(file_path)
    except Exception:
        return str(file_path)


def _execute_caliper_command(
    command: list[str],
    step_name: str,
    status_file: Path,
    step_logs_dir: Path,
) -> tuple[subprocess.CompletedProcess, dict[str, Any], Path]:
    """Execute a Caliper CLI command and return result and status data.

    Args:
        command: CLI command arguments list
        step_name: Name for logging banners (e.g., "caliper parse", "caliper visualize")
        status_file: Path to read status YAML from
        step_logs_dir: Directory to create log file in

    Returns:
        Tuple of (subprocess result, parsed status data, log file path)
    """
    from projects.caliper.orchestration.cli_builder import log_caliper_start_banner
    from projects.caliper.orchestration.postprocess_logging import (
        _handle_caliper_output_and_completion_with_header,
        _write_step_footer_to_log_file,
        _write_step_header_to_log_file,
    )
    from projects.caliper.orchestration.step_logging import _get_next_step_index

    # Create log file path with proper step index
    step_index = _get_next_step_index(step_logs_dir)
    step_name_safe = step_name.replace(" ", "_")
    log_file = step_logs_dir / f"{step_index:03d}__{step_name_safe}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Create script file for debugging in step_scripts directory with index prefix
    step_scripts_dir = env.ARTIFACT_DIR / "step_scripts"
    step_scripts_dir.mkdir(parents=True, exist_ok=True)
    script_file = step_scripts_dir / f"{step_index:03d}__{step_name_safe}.sh"

    # Log start banner and save script
    log_caliper_start_banner(command, script_file, step_name.upper())

    # Write header to log file
    _write_step_header_to_log_file(log_file, command, step_name.upper())

    # Execute command with combined stdout/stderr
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Combine stderr into stdout
        text=True,
    )

    # Handle output, status parsing, and log completion banner
    status_data = _handle_caliper_output_and_completion_with_header(
        result, log_file, status_file, step_name.upper()
    )

    # Write footer to log file
    _write_step_footer_to_log_file(log_file, result, step_name.upper())

    return result, status_data, log_file


def run_artifacts_to_kpis(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    plugin,
    model,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Generate KPI JSON using fork/exec subprocess execution."""

    if not postprocess_config.kpi.enabled:
        return {
            "status": "disabled",
            "reason": "kpi disabled",
            "completed_at": time.time(),
            "log_file": None,
        }
    if not postprocess_config.kpi.artifacts_to_kpis.enabled:
        return {
            "status": "disabled",
            "reason": "kpi.artifacts_to_kpis disabled",
            "completed_at": time.time(),
            "log_file": None,
        }

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.kpi.artifacts_to_kpis.output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Create temporary status file for subprocess communication
        status_file = output_dir / "kpi_generate_status.yaml"

        # Build CLI command
        command = build_kpi_generate_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi generate",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass

        # Convert to expected format
        if result.returncode == 0 and status_data.get("success"):
            relative_path = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)
            logger.info(
                f"KPI generate: output_file={output_file}, env.ARTIFACT_DIR={env.ARTIFACT_DIR}, relative_path={relative_path}"
            )
            return {
                "status": "success",
                "output_file": relative_path,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            return {
                "status": "failed",
                "error": status_data.get("error", "Unknown error"),
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        # Log the full traceback to help with debugging
        full_traceback = traceback.format_exc()
        logger.error(f"KPI generation failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        return {"status": "failed", "error": str(e), "completed_at": time.time(), "log_file": None}


def run_artifacts_to_ai_data(
    postprocess_config,
    plugin,
    model,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Export AI evaluation data using fork/exec subprocess execution."""

    if not postprocess_config.kpi.artifacts_to_ai_data.enabled:
        return {
            "status": "disabled",
            "reason": "kpi.artifacts_to_ai_data disabled",
            "completed_at": time.time(),
            "log_file": None,
        }

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.kpi.artifacts_to_ai_data.output_dir
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Create temporary status file for subprocess communication
        status_file = output_dir / "ai_eval_export_status.yaml"

        # Build CLI command
        command = build_ai_eval_export_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper ai-eval-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass

        # Convert to expected format
        if result.returncode == 0 and status_data.get("success"):
            relative_path = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)
            return {
                "status": "success",
                "output_file": relative_path,
                "ai_data_dir": relative_path,  # Expected by postprocess.py
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            return {
                "status": "failed",
                "error": status_data.get("error", "Unknown error"),
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        # Log the full traceback to help with debugging
        full_traceback = traceback.format_exc()
        logger.error(f"AI evaluation export failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        return {"status": "failed", "error": str(e), "completed_at": time.time(), "log_file": None}


def run_kpis_to_csv(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    plugin,
    model,
    output_dir: Path,
    kpi_json_path: Path,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Export KPI JSON to CSV using fork/exec subprocess execution."""

    if not postprocess_config.kpi.csv.enabled:
        return {
            "status": "disabled",
            "reason": "kpi.csv disabled",
            "completed_at": time.time(),
            "log_file": None,
        }

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.kpi.csv.output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Create temporary status file for subprocess communication
        status_file = output_dir / "kpi_csv_export_status.yaml"

        # Build CLI command
        command = build_kpi_csv_export_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
            kpi_json_path=kpi_json_path,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi csv-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass

        # Convert to expected format
        if result.returncode == 0 and status_data.get("success"):
            relative_path = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)
            return {
                "status": "success",
                "output_file": relative_path,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            return {
                "status": "failed",
                "error": status_data.get("error", "Unknown error"),
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        # Log the full traceback to help with debugging
        full_traceback = traceback.format_exc()
        logger.error(f"KPI CSV export failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        return {"status": "failed", "error": str(e), "completed_at": time.time(), "log_file": None}


def run_analyse_kpis(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    current_kpis_file: Path,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Analyze KPIs using fork/exec subprocess execution."""

    if not postprocess_config.analyze.enabled:
        return {
            "status": "disabled",
            "reason": "analyze disabled",
            "completed_at": time.time(),
            "log_file": None,
        }

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.analyze.output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Calculate historical KPIs directory path
        historical_kpis_dir = Path(postprocess_config.analyze.historical_kpis)
        if not historical_kpis_dir.is_absolute():
            historical_kpis_dir = output_dir / historical_kpis_dir

        # Create temporary status file for subprocess communication
        status_file = output_dir / "analyse_kpis_status.yaml"

        # Build CLI command
        command = build_analyse_kpis_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
            current_kpis_file=current_kpis_file,
            historical_kpis_dir=historical_kpis_dir,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi analyse-kpis",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass

        # Convert to expected format
        if result.returncode == 0 and status_data.get("success"):
            return {
                "status": "success",
                "output_file": _make_path_relative_to_base(output_file, env.ARTIFACT_DIR),
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            return {
                "status": "failed",
                "error": status_data.get("error", "Unknown error"),
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("KPI analysis failed in run_analyse_kpis")
        return {"status": "failed", "error": str(e), "completed_at": time.time(), "log_file": None}


def run_parse_step(
    config: CaliperOrchestrationPostprocessConfig,
    tree_root: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """Execute the parse step if enabled.

    Returns:
        Tuple of (success, step_result)
    """
    if not config.parse.enabled:
        return True, {"status": "disabled", "reason": "parse disabled", "completed_at": time.time()}

    # Create status file for CLI output
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as status_f:
        status_file = Path(status_f.name)

    try:
        # Build CLI command
        command = build_parse_command(
            config=config,
            tree_root=tree_root,
            manifest_path=manifest_path,
            status_file=status_file,
            use_cache=not config.parse.no_cache,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper parse",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return True, {
                "status": "success",
                "detail": "Parse completed successfully",
                "test_directories": status_data.get("test_directories", []),
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("Caliper parse failed: %s", error_msg)
            return False, {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("Parse step execution failed")
        return False, {
            "status": "failed",
            "detail": f"Exception during parse: {str(e)}",
            "completed_at": time.time(),
        }
    finally:
        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass


def run_visualize_step(
    config: CaliperOrchestrationPostprocessConfig,
    tree_root: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
    output_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """Execute the visualize step if enabled.

    Returns:
        Tuple of (success, step_result)
    """
    if not config.visualize.enabled:
        return True, {
            "status": "disabled",
            "reason": "visualize disabled",
            "completed_at": time.time(),
        }

    # Create status file for CLI output
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as status_f:
        status_file = Path(status_f.name)

    try:
        # Build CLI command
        command = build_visualize_command(
            config=config,
            tree_root=tree_root,
            manifest_path=manifest_path,
            status_file=status_file,
            output_dir=output_dir,
            use_cache=not config.parse.no_cache,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper visualize",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return True, {
                "status": "success",
                "detail": "Visualize completed successfully",
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("Caliper visualize failed: %s", error_msg)
            return False, {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("Visualize step execution failed")
        return False, {
            "status": "failed",
            "detail": f"Exception during visualize: {str(e)}",
            "completed_at": time.time(),
        }
    finally:
        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass


def run_s3_import_step(
    config: CaliperOrchestrationPostprocessConfig,
    output_dir: Path,
    step_logs_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """Execute the S3 import step.

    Returns:
        Tuple of (success, step_result)
    """
    if not config.s3.import_.enabled:
        return True, {
            "status": "disabled",
            "reason": "s3_import disabled",
            "completed_at": time.time(),
        }

    # Create status file for CLI output
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as status_f:
        status_file = Path(status_f.name)

    try:
        # Build CLI command
        command = build_s3_import_command(
            config=config,
            status_file=status_file,
            output_dir=output_dir,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper s3-import",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return True, {
                "status": "success",
                "detail": "S3 import completed successfully",
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("S3 import failed: %s", error_msg)
            return False, {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("S3 import step execution failed")
        return False, {
            "status": "failed",
            "detail": f"Exception during s3-import: {str(e)}",
            "completed_at": time.time(),
        }
    finally:
        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass


def run_s3_export_step(
    config: CaliperOrchestrationPostprocessConfig,
    step_logs_dir: Path,
    kpis_file: Path | None = None,
    csv_file: Path | None = None,
    ai_data_dir: Path | None = None,
    analysis_file: Path | None = None,
) -> dict[str, Any]:
    """Execute the S3 export step.

    Returns:
        Step result dictionary
    """
    if not config.s3.export.enabled:
        return {
            "status": "disabled",
            "reason": "s3_export disabled",
            "completed_at": time.time(),
        }

    # Create status file for CLI output
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as status_f:
        status_file = Path(status_f.name)

    try:
        # Build CLI command
        command = build_s3_export_command(
            config=config,
            status_file=status_file,
            kpis_file=kpis_file,
            csv_file=csv_file,
            ai_data_dir=ai_data_dir,
            analysis_file=analysis_file,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper s3-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return {
                "status": "success",
                "detail": "S3 export completed successfully",
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("S3 export failed: %s", error_msg)
            return {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("S3 export step execution failed")
        return {
            "status": "failed",
            "detail": f"Exception during s3-export: {str(e)}",
            "completed_at": time.time(),
        }
    finally:
        # Clean up temporary status file
        try:
            status_file.unlink()
        except FileNotFoundError:
            pass
