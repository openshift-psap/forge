"""
Config-driven Caliper artifact replot for FORGE orchestration projects.

Handles MLflow artifact downloading and post-processing pipeline execution.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from projects.caliper.orchestration.postprocess import run_postprocess_from_orchestration_config
from projects.caliper.orchestration.postprocess_outcome import TestPhaseOutcome
from projects.core.library import vault as vault_lib

logger = logging.getLogger(__name__)


def _get_step_from_list(steps_list: list, step_name: str) -> dict:
    """
    Get a step result by name from the list-based steps structure.

    Args:
        steps_list: List of step dictionaries with step name as key
        step_name: Name of the step to retrieve

    Returns:
        Step dictionary if found, empty dict if not found
    """
    for step in steps_list:
        if step_name in step:
            return step[step_name]
    return {}


def _download_mlflow_artifacts_via_import(
    replot_url: str,
    replot_download_dir: Path,
    mlflow_secrets_path: Path,
) -> dict:
    """
    Download MLflow artifacts from a replot URL using the artifacts import command.

    Args:
        replot_url: MLflow URL containing run ID
        replot_download_dir: Local directory to download artifacts to
        mlflow_secrets_path: Path to MLflow secrets file

    Returns:
        Dict containing download status information

    Raises:
        ValueError: If URL parsing fails
        RuntimeError: If import command fails
    """
    import subprocess
    import sys

    # Extract run ID for status reporting
    run_id_match = re.search(r"[/#]runs/([^/?#]+)", replot_url)
    if not run_id_match:
        raise ValueError(f"Could not parse MLflow run ID from URL: {replot_url}")

    run_id = run_id_match.group(1)

    logger.info(f"Downloading artifacts using import command for run ID: {run_id}")

    # Construct command to call the existing artifacts import
    cmd = [
        sys.executable,
        "-m",
        "projects.caliper.cli.main",
        "artifacts",
        "import",
        "--from-mlflow-url",
        replot_url,
        "--output-dir",
        str(replot_download_dir),
        "--mlflow-secrets",
        str(mlflow_secrets_path),
        "--mlflow-insecure-tls",
    ]

    logger.info(f"Running import command: {' '.join(cmd)}")
    logger.info(f"Target download directory: {replot_download_dir}")

    # Record what exists before import
    files_before = set()
    if replot_download_dir.exists():
        files_before = set(replot_download_dir.rglob("*"))
        files_before = {f for f in files_before if f.is_file()}

    try:
        # Run the import command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )

        logger.info("Import command completed successfully")
        if result.stdout:
            logger.debug(f"Import stdout: {result.stdout}")
        if result.stderr:
            logger.debug(f"Import stderr: {result.stderr}")

        # Find what was actually downloaded
        files_after = set()
        if replot_download_dir.exists():
            files_after = set(replot_download_dir.rglob("*"))
            files_after = {f for f in files_after if f.is_file()}

        downloaded_files = list(files_after - files_before)

        if not downloaded_files:
            raise RuntimeError(
                f"Import command completed but no new files found in {replot_download_dir}"
            )

        logger.info(f"Downloaded {len(downloaded_files)} files to {replot_download_dir}")
        if downloaded_files:
            logger.info("Downloaded files:")
            for file in downloaded_files[:10]:  # Show first 10
                try:
                    relative_path = file.relative_to(replot_download_dir)
                    logger.info(f"  {relative_path}")
                except ValueError:
                    logger.info(f"  {file}")
            if len(downloaded_files) > 10:
                logger.info(f"  ... and {len(downloaded_files) - 10} more")

        return {
            "download_status": "success",
            "downloaded_files": len(downloaded_files),
            "run_id": run_id,
            "import_command": " ".join(cmd),
        }

    except subprocess.CalledProcessError as e:
        error_msg = f"Import command failed with exit code {e.returncode}"
        if e.stdout:
            error_msg += f"\nStdout: {e.stdout}"
        if e.stderr:
            error_msg += f"\nStderr: {e.stderr}"

        logger.error(error_msg)
        raise RuntimeError(f"MLflow artifact download via import failed: {error_msg}") from e
    except Exception as e:
        raise RuntimeError(f"MLflow artifact download via import failed: {e}") from e


def run_replot_from_orchestration_config(
    replot_url: str,
    artifact_directory: Path,
    vault_name: str,
    vault_mlflow_secret: str,
    keep_replot_dir: bool = False,
    postprocess_config: dict | None = None,
) -> dict:
    """
    Run replotting logic with orchestration configuration.

    Note: Insecure TLS is enabled by default for MLflow connections to support
    servers with self-signed certificates.

    Args:
        replot_url: MLflow URL to download artifacts from
        artifact_directory: Directory for final artifacts output
        vault_name: Name of the vault containing secrets
        vault_mlflow_secret: MLflow secret key in the vault
        keep_replot_dir: Whether to keep the download directory after processing
        postprocess_config: Configuration for post-processing

    Returns:
        Dict containing replot operation status and results
    """
    replot_download_dir = artifact_directory / "replot"

    # Check if URL is valid for downloading
    skip_download = (
        not replot_url or not replot_url.strip() or replot_url.strip().lower() in ("false", "0")
    )

    if skip_download:
        keep_replot_dir = True
        logger.info("No replot URL provided, skipping artifact download")
        logger.info("Automatically setting keep_replot_dir=True to preserve existing files")
        logger.info(f"Download directory: {replot_download_dir}")
        logger.info(f"Output directory: {artifact_directory}")
    else:
        logger.info(f"Replotting artifacts from URL: {replot_url}")
        logger.info(f"Download directory: {replot_download_dir}")
        logger.info(f"Output directory: {artifact_directory}")

        # Get MLflow secrets from vault
        mlflow_secrets_path = vault_lib.get_vault_content_path(vault_name, vault_mlflow_secret)
        if mlflow_secrets_path is None or not mlflow_secrets_path.exists():
            raise FileNotFoundError(
                f"MLflow secrets not found in vault {vault_name}/{vault_mlflow_secret}"
            )

        logger.info(f"Using MLflow secrets from vault {vault_name}/{vault_mlflow_secret}")

    status = {
        "replot": {
            "url": replot_url,
            "download_directory": str(replot_download_dir),
            "output_directory": str(artifact_directory),
            "keep_download_dir": keep_replot_dir,
        }
    }

    try:
        # Step 1: Download artifacts (or skip if no URL)
        if skip_download:
            logger.info("Skipping download step (no URL provided)")

            # Check if download directory already exists with content
            if replot_download_dir.exists() and any(replot_download_dir.iterdir()):
                existing_files = list(replot_download_dir.rglob("*"))
                existing_files = [f for f in existing_files if f.is_file()]

                logger.info(f"Found {len(existing_files)} existing files in download directory")
                if existing_files:
                    logger.info("Existing files:")
                    for file in existing_files[:10]:  # Show first 10
                        try:
                            relative_path = file.relative_to(replot_download_dir)
                            logger.info(f"  {relative_path}")
                        except ValueError:
                            logger.info(f"  {file}")
                    if len(existing_files) > 10:
                        logger.info(f"  ... and {len(existing_files) - 10} more")

                status["replot"].update(
                    {
                        "download_status": "skipped",
                        "downloaded_files": len(existing_files),
                        "note": "no_url_provided",
                    }
                )
            else:
                logger.info("No existing artifacts found to process")
                status["replot"].update(
                    {
                        "download_status": "skipped",
                        "downloaded_files": 0,
                        "note": "no_url_provided",
                    }
                )
        else:
            logger.info("Downloading artifacts...")

            # Check if download directory already exists with content
            if replot_download_dir.exists() and any(replot_download_dir.iterdir()):
                logger.info(
                    f"Replot download directory already exists with content, skipping download: {replot_download_dir}"
                )

                # Count existing files for status
                existing_files = list(replot_download_dir.rglob("*"))
                existing_files = [f for f in existing_files if f.is_file()]

                logger.info(f"Found {len(existing_files)} existing files")
                if existing_files:
                    logger.info("Existing files:")
                    for file in existing_files[:10]:  # Show first 10
                        try:
                            relative_path = file.relative_to(replot_download_dir)
                            logger.info(f"  {relative_path}")
                        except ValueError:
                            logger.info(f"  {file}")
                    if len(existing_files) > 10:
                        logger.info(f"  ... and {len(existing_files) - 10} more")

                status["replot"].update(
                    {
                        "download_status": "skipped",
                        "downloaded_files": len(existing_files),
                        "note": "directory_already_exists",
                    }
                )
            else:
                # Create the download directory
                replot_download_dir.mkdir(parents=True, exist_ok=True)

                # Download artifacts based on URL type
                if "mlflow" in replot_url.lower() and "runs" in replot_url:
                    download_result = _download_mlflow_artifacts_via_import(
                        replot_url, replot_download_dir, mlflow_secrets_path
                    )
                    status["replot"].update(download_result)
                else:
                    raise ValueError(
                        f"Unsupported replot URL type: {replot_url}. Only MLflow URLs are currently supported."
                    )

        logger.info("Download step completed")

        # Step 2: Run post-processing on artifacts
        if artifact_directory.exists():
            logger.info("Running post-processing...")

            visualize_output_dir = artifact_directory / "postprocess_output"
            visualize_output_dir.mkdir(parents=True, exist_ok=True)

            postprocess_result = run_postprocess_from_orchestration_config(
                postprocess_config_raw=postprocess_config or {},
                artifacts_dir=artifact_directory,
                visualize_output_dir=visualize_output_dir,
                test_outcome=TestPhaseOutcome("SUCCESS"),
            )
        else:
            logger.info("Artifacts directory not found")
            postprocess_result = {
                "success": True,
                "steps": [
                    {
                        "visualize": {
                            "status": "skipped",
                            "message": "No artifacts found",
                            "artifact_directory": str(artifact_directory),
                        }
                    }
                ],
            }

        # Log post-processing results
        steps_list = postprocess_result.get("steps", [])
        visualize_step = _get_step_from_list(steps_list, "visualize")

        if visualize_step.get("status") == "skipped":
            logger.info("Post-processing completed (visualizations skipped)")
        elif visualize_step.get("paths"):
            viz_paths = visualize_step["paths"]
            logger.info(f"Post-processing completed with {len(viz_paths)} visualizations generated")
        else:
            logger.info("Post-processing completed (parsing only)")

        postprocess_success = postprocess_result.get("success", False)
        status["replot"]["postprocess_status"] = "success" if postprocess_success else "failed"
        status["replot"]["postprocess_result"] = postprocess_result

        # Step 3: Clean up download directory unless keeping
        if not keep_replot_dir and replot_download_dir.exists():
            logger.info(f"Cleaning up download directory: {replot_download_dir}")
            shutil.rmtree(replot_download_dir)
            status["replot"]["cleanup_status"] = "completed"
        elif keep_replot_dir and replot_download_dir.exists():
            logger.info(f"Keeping download directory as requested: {replot_download_dir}")
            status["replot"]["cleanup_status"] = "skipped"
        else:
            logger.info("No download directory to clean up")
            status["replot"]["cleanup_status"] = "not_needed"

        # Overall status considers both download and postprocessing
        if postprocess_success:
            status["replot"]["status"] = "success"
            status["replot"]["message"] = "Replot completed successfully"
        else:
            status["replot"]["status"] = "failed"
            status["replot"]["message"] = "Replot completed but postprocessing failed"

    except Exception as e:
        logger.error(f"Replot failed: {e}")
        status["replot"]["status"] = "failed"
        status["replot"]["message"] = str(e)

        # Try to clean up on failure unless keeping
        if not keep_replot_dir and replot_download_dir.exists():
            try:
                shutil.rmtree(replot_download_dir)
                status["replot"]["cleanup_status"] = "completed_on_failure"
            except Exception as cleanup_e:
                logger.warning(f"Failed to cleanup download directory: {cleanup_e}")
                status["replot"]["cleanup_status"] = "failed"

        raise

    return status
