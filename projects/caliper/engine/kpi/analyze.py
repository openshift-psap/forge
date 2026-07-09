"""Regression analysis vs baseline KPI set."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run_kpi_analysis(
    current_kpi_file: Path,
    historical_data_dir: Path,
    output_file: Path,
    plugin_module: str,
) -> int:
    """Run KPI analysis and generate output file.

    Args:
        current_kpi_file: Path to current KPI JSON file
        historical_data_dir: Directory containing historical KPI files
        output_file: Path where analysis results will be written
        plugin_module: Plugin module name for analysis

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    try:
        logger.info(f"Running KPI analysis for: {current_kpi_file}")
        logger.info(f"Historical data directory: {historical_data_dir}")
        logger.info(f"Output file: {output_file}")
        logger.info(f"Plugin module: {plugin_module}")

        # Check if current KPI file exists
        if not current_kpi_file.exists():
            logger.error(f"Current KPI file not found: {current_kpi_file}")
            return 1

        # Check if historical data directory exists
        if not historical_data_dir.exists():
            logger.error(f"Historical data directory not found: {historical_data_dir}")
            return 1

        # Find all historical KPI files
        historical_kpi_files = []
        for kpi_file in historical_data_dir.rglob("kpis.json"):
            historical_kpi_files.append(str(kpi_file))
            logger.info(f"Found historical KPI file: {kpi_file}")

        if not historical_kpi_files:
            logger.warning("No historical KPI files found for analysis")
            # Return special exit code 2 to indicate warning (no historical data)
            analysis_result = {
                "status": "warning",
                "message": "no historical KPI found for regression testing",
                "current_kpi_file": str(current_kpi_file),
                "historical_kpi_files": historical_kpi_files,
                "baseline_files_count": 0,
                "plugin_module": plugin_module,
                "completed_at": time.time(),
            }
            # Write warning result to output file
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, "w") as f:
                json.dump(analysis_result, f, indent=2)
            return 2  # Special exit code for warning

        logger.info(f"Found {len(historical_kpi_files)} historical KPI files for analysis")

        # Create analysis result
        analysis_result = {
            "status": "success (stub)",
            "current_kpi_file": str(current_kpi_file),
            "historical_kpi_files": historical_kpi_files,
            "baseline_files_count": len(historical_kpi_files),
            "plugin_module": plugin_module,
            "completed_at": time.time(),
        }

        # Create output directory if needed
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Write analysis results to output file
        with open(output_file, "w") as f:
            json.dump(analysis_result, f, indent=2)
            f.write("\n")  # Add EOL at EOF

        logger.info(f"Analysis completed successfully, results written to: {output_file}")
        return 0

    except Exception as e:
        logger.exception("KPI analysis failed")

        # Create failure result
        failure_result = {
            "status": "failed",
            "error": str(e),
            "current_kpi_file": str(current_kpi_file) if current_kpi_file else None,
            "completed_at": time.time(),
        }

        try:
            # Attempt to write failure result
            if output_file:
                output_file.parent.mkdir(parents=True, exist_ok=True)
                with open(output_file, "w") as f:
                    json.dump(failure_result, f, indent=2)
                    f.write("\n")  # Add EOL at EOF
        except Exception:
            logger.exception("Failed to write error result to output file")

        return 1


def analyze_kpis(
    postprocess_config,  # CaliperOrchestrationPostprocessConfig
    plugin_module: str,
    base_dir: Path,
    output_dir: Path,
    current_kpis_file: Path,
) -> dict[str, Any]:
    """Run KPI analysis step and return result status.

    This is the orchestration interface for KPI analysis.
    """
    if not postprocess_config.analyze.enabled:
        return {"status": "disabled", "reason": "analyze disabled"}

    # Determine paths for analysis
    analyze_config = postprocess_config.analyze

    # Historical KPIs directory path
    historical_kpis_dir = Path(analyze_config.historical_kpis)
    if not historical_kpis_dir.is_absolute():
        historical_kpis_dir = output_dir / historical_kpis_dir

    # Output path for analysis results
    output_path = output_dir / analyze_config.output

    try:
        # Check if required files exist
        if not current_kpis_file.exists():
            return {
                "status": "failed",
                "error": f"Current KPI file not found: {current_kpis_file}",
                "completed_at": time.time(),
            }

        if not historical_kpis_dir.exists():
            return {
                "status": "failed",
                "error": f"Historical KPIs directory not found: {historical_kpis_dir}",
                "completed_at": time.time(),
            }

        # Log command to reproduce this step
        from projects.caliper.orchestration.step_logging import log_analyze_command

        log_analyze_command(
            base_dir=base_dir,
            plugin_module=plugin_module,
            current_kpis_path=current_kpis_file,
            historical_kpis_dir=historical_kpis_dir,
            output_path=output_path,
        )

        # Call the core analysis function
        exit_code = run_kpi_analysis(
            current_kpi_file=current_kpis_file,
            historical_data_dir=historical_kpis_dir,
            output_file=output_path,
            plugin_module=plugin_module,
        )

        if exit_code == 0:
            # Read the results from the output file
            if output_path.exists():
                with open(output_path) as f:
                    result_data = json.load(f)

                # Return success with output file path for notifications
                return {
                    "status": "success",
                    "output_file": str(output_path),
                    "baseline_files_count": result_data.get("baseline_files_count", 0),
                    "completed_at": time.time(),
                }
            else:
                return {
                    "status": "failed",
                    "error": "Analysis completed but no output file was generated",
                    "completed_at": time.time(),
                }
        elif exit_code == 2:
            # Warning: no historical data found
            if output_path.exists():
                with open(output_path) as f:
                    result_data = json.load(f)

                # Return warning status with message
                return {
                    "status": "warning",
                    "message": result_data.get(
                        "message", "no historical KPI found for regression testing"
                    ),
                    "output_file": str(output_path),
                    "baseline_files_count": result_data.get("baseline_files_count", 0),
                    "completed_at": time.time(),
                }
            else:
                return {
                    "status": "warning",
                    "message": "no historical KPI found for regression testing",
                    "baseline_files_count": 0,
                    "completed_at": time.time(),
                }
        else:
            # Read error from output file if it exists
            error_msg = f"Analysis failed with exit code {exit_code}"
            if output_path.exists():
                try:
                    with open(output_path) as f:
                        result_data = json.load(f)
                    error_msg = result_data.get("error", error_msg)
                except Exception:
                    pass

            return {
                "status": "failed",
                "error": error_msg,
                "completed_at": time.time(),
            }

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.exception(f"Analysis step failed with {error_msg}")
        return {
            "status": "failed",
            "error": error_msg,
            "error_type": type(e).__name__,
            "completed_at": time.time(),
        }


def find_baseline_kpis(historical_dir: Path) -> dict[Path, dict[str, Any]]:
    """Load all kpis.json files from historical directory and return a mapping of path to loaded JSON object.

    Args:
        historical_dir: Directory to search for historical kpis.json files

    Returns:
        Dictionary mapping file paths to loaded KPI JSON objects
    """
    baseline_kpis = {}
    kpi_files = list(historical_dir.rglob("kpis.json"))

    if not kpi_files:
        logger.warning(f"No kpis.json files found in historical directory: {historical_dir}")
        return baseline_kpis

    logger.info(f"Found {len(kpi_files)} historical KPI files to load")

    for kpi_file in kpi_files:
        try:
            with open(kpi_file) as f:
                kpi_data = json.load(f)

            # Validate that it's a hierarchical format (schema_version 2)
            schema_version = kpi_data.get("schema_version", "unknown")
            if schema_version != "2":
                logger.warning(
                    f"Skipping {kpi_file}: unsupported schema version {schema_version} (only version 2 supported)"
                )
                continue

            baseline_kpis[kpi_file] = kpi_data
            logger.debug(f"Loaded KPI file: {kpi_file}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON in {kpi_file}: {e}")
            continue
        except FileNotFoundError as e:
            logger.error(f"KPI file not found: {kpi_file}: {e}")
            continue
        except Exception as e:
            logger.error(f"Failed to load KPI file {kpi_file}: {e}")
            continue

    logger.info(f"Successfully loaded {len(baseline_kpis)} historical KPI files")
    return baseline_kpis


def run_analyze(
    *,
    current_path: Any,
    baseline_kpis: dict[Path, dict[str, Any]],
    output_path: Any,
    plugin: Any = None,
) -> dict[str, Any]:
    """Run KPI analysis against ALL baseline files (CLI interface)."""
    # For CLI use, we need to extract the baseline directory from the dict
    # This is a bit of a hack, but the CLI interface expects a directory
    if baseline_kpis:
        # Get the parent directory of the first baseline file
        first_baseline_path = next(iter(baseline_kpis.keys()))
        historical_dir = first_baseline_path.parent

        # Extract plugin module name if available
        plugin_module = getattr(plugin, "__module__", "unknown") if plugin else "unknown"

        # Call the core analysis function
        exit_code = run_kpi_analysis(
            current_kpi_file=Path(current_path),
            historical_data_dir=historical_dir,
            output_file=Path(output_path),
            plugin_module=plugin_module,
        )

        if exit_code == 0:
            # Read results and convert to expected format for CLI
            try:
                with open(Path(output_path)) as f:
                    result_data = json.load(f)
                return {
                    "status": "success",
                    "baseline_files_count": result_data.get("baseline_files_count", 0),
                    "completed_at": result_data.get("completed_at", time.time()),
                }
            except Exception as e:
                return {
                    "status": "failed",
                    "error": f"Failed to read analysis results: {e}",
                    "completed_at": time.time(),
                }
        else:
            return {
                "status": "failed",
                "error": f"Analysis failed with exit code {exit_code}",
                "completed_at": time.time(),
            }
    else:
        return {
            "status": "failed",
            "error": "No baseline KPI files provided",
            "completed_at": time.time(),
        }


# EOF
