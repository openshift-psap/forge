"""
Consolidated replot orchestration for FORGE projects.

Handles MLflow artifact downloading, Prometheus TSDB replay, and
post-processing pipeline execution.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import click

from projects.caliper.orchestration.postprocess import run_postprocess_from_orchestration_config
from projects.caliper.orchestration.postprocess_outcome import TestPhaseOutcome
from projects.cluster.library.prom.replay import replay_metrics_from_config
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.core.library import vault as vault_lib
from projects.core.library.ci import ExitCategory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_step_from_list(steps_list: list, step_name: str) -> dict:
    for step in steps_list:
        if step_name in step:
            return step[step_name]
    return {}


def _download_mlflow_artifacts_via_import(
    replot_url: str,
    replot_download_dir: Path,
    mlflow_secrets_path: Path,
) -> dict:
    run_id_match = re.search(r"[/#]runs/([^/?#]+)", replot_url)
    if not run_id_match:
        raise ValueError(f"Could not parse MLflow run ID from URL: {replot_url}")

    run_id = run_id_match.group(1)

    logger.info("Downloading artifacts using import command for run ID: %s", run_id)

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

    logger.info("Running import command: %s", " ".join(cmd))
    logger.info("Target download directory: %s", replot_download_dir)

    files_before: set[Path] = set()
    if replot_download_dir.exists():
        files_before = {f for f in replot_download_dir.rglob("*") if f.is_file()}

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        logger.info("Import command completed successfully")
        if result.stdout:
            logger.debug("Import stdout: %s", result.stdout)
        if result.stderr:
            logger.debug("Import stderr: %s", result.stderr)

        files_after: set[Path] = set()
        if replot_download_dir.exists():
            files_after = {f for f in replot_download_dir.rglob("*") if f.is_file()}

        downloaded_files = list(files_after - files_before)

        if not downloaded_files:
            raise RuntimeError(
                f"Import command completed but no new files found in {replot_download_dir}"
            )

        logger.info("Downloaded %d files to %s", len(downloaded_files), replot_download_dir)
        for file in downloaded_files[:10]:
            try:
                logger.info("  %s", file.relative_to(replot_download_dir))
            except ValueError:
                logger.info("  %s", file)
        if len(downloaded_files) > 10:
            logger.info("  ... and %d more", len(downloaded_files) - 10)

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


def _log_existing_files(directory: Path, limit: int = 10) -> list[Path]:
    existing_files = [f for f in directory.rglob("*") if f.is_file()]
    logger.info("Found %d existing files", len(existing_files))
    for file in existing_files[:limit]:
        try:
            logger.info("  %s", file.relative_to(directory))
        except ValueError:
            logger.info("  %s", file)
    if len(existing_files) > limit:
        logger.info("  ... and %d more", len(existing_files) - limit)
    return existing_files


# ---------------------------------------------------------------------------
# Download step
# ---------------------------------------------------------------------------


def _run_download_step(
    replot_url: str,
    replot_download_dir: Path,
    vault_name: str | None,
    vault_mlflow_secret: str | None,
    status: dict,
) -> None:
    skip_download = (
        not replot_url or not replot_url.strip() or replot_url.strip().lower() in ("false", "0")
    )

    if skip_download:
        logger.warning("No replot URL provided, skipping artifact download")
        if replot_download_dir.exists() and any(replot_download_dir.iterdir()):
            existing = _log_existing_files(replot_download_dir)
            status["replot"].update(
                {
                    "download_status": "skipped",
                    "downloaded_files": len(existing),
                    "note": "no_url_provided",
                }
            )
        else:
            logger.info("No existing artifacts found to process")
            status["replot"].update(
                {"download_status": "skipped", "downloaded_files": 0, "note": "no_url_provided"}
            )
        return

    logger.info("Replotting artifacts from URL: %s", replot_url)
    logger.info("Download directory: %s", replot_download_dir)

    mlflow_secrets_path = vault_lib.get_vault_content_path(vault_name, vault_mlflow_secret)
    if mlflow_secrets_path is None or not mlflow_secrets_path.exists():
        raise FileNotFoundError(
            f"MLflow secrets not found in vault {vault_name}/{vault_mlflow_secret}"
        )

    if replot_download_dir.exists() and any(replot_download_dir.iterdir()):
        logger.info(
            "Replot download directory already exists with content, skipping download: %s",
            replot_download_dir,
        )
        existing = _log_existing_files(replot_download_dir)
        status["replot"].update(
            {
                "download_status": "skipped",
                "downloaded_files": len(existing),
                "note": "directory_already_exists",
            }
        )
        return

    replot_download_dir.mkdir(parents=True, exist_ok=True)

    if "mlflow" in replot_url.lower() and "runs" in replot_url:
        download_result = _download_mlflow_artifacts_via_import(
            replot_url, replot_download_dir, mlflow_secrets_path
        )
        status["replot"].update(download_result)
    else:
        raise ValueError(
            f"Unsupported replot URL type: {replot_url}. Only MLflow URLs are currently supported."
        )


# ---------------------------------------------------------------------------
# Replay step
# ---------------------------------------------------------------------------


def _run_replay_step(artifact_directory: Path, status: dict) -> None:
    try:
        if not config.project.get_config(
            "prom.capture.metrics.enabled", False, print=False, warn=False
        ):
            status["replot"]["replay_status"] = "skipped"
            return

        force_refresh = config.project.get_config(
            "prom.capture.metrics.force_refresh", False, print=False, warn=False
        )
        logger.info("Checking for Prometheus metrics to replay...")
        replay_errors = replay_metrics_from_config(artifact_directory, force_refresh=force_refresh)

        if replay_errors:
            logger.warning("Prometheus replay had errors for groups: %s", ", ".join(replay_errors))
            status["replot"]["replay_errors"] = replay_errors

        status["replot"]["replay_status"] = "completed"
    except Exception as e:
        logger.warning("Prometheus replay failed (non-fatal): %s", e)
        status["replot"]["replay_status"] = "failed"
        status["replot"]["replay_error"] = str(e)


# ---------------------------------------------------------------------------
# Post-processing step
# ---------------------------------------------------------------------------


def _run_postprocess_step(
    artifact_directory: Path,
    postprocess_config: dict | None,
    status: dict,
) -> None:
    if not artifact_directory.exists():
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
    else:
        logger.info("Running post-processing...")
        with env.TempArtifactDir(artifact_directory / "postprocess_output"):
            postprocess_result = run_postprocess_from_orchestration_config(
                postprocess_config_raw=postprocess_config or {},
                artifacts_dir=artifact_directory,
                output_dir=None,
                test_outcome=TestPhaseOutcome("SUCCESS"),
            )

    steps_list = postprocess_result.get("steps", [])
    visualize_step = _get_step_from_list(steps_list, "visualize")

    if visualize_step.get("status") == "skipped":
        logger.info("Post-processing completed (visualizations skipped)")
    elif visualize_step.get("paths"):
        logger.info(
            "Post-processing completed with %d visualizations generated",
            len(visualize_step["paths"]),
        )
    else:
        logger.info("Post-processing completed (parsing only)")

    postprocess_success = postprocess_result.get("success", False)
    status["replot"]["postprocess_status"] = "success" if postprocess_success else "failed"
    status["replot"]["postprocess_result"] = postprocess_result

    return postprocess_success


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_replot_from_orchestration_config(
    replot_url: str,
    artifact_directory: Path,
    vault_name: str | None,
    vault_mlflow_secret: str | None,
    keep_replot_dir: bool = False,
    postprocess_config: dict | None = None,
) -> dict:
    replot_download_dir = artifact_directory / "replot"

    skip_download = (
        not replot_url or not replot_url.strip() or replot_url.strip().lower() in ("false", "0")
    )
    if skip_download:
        keep_replot_dir = True

    logger.info("Output directory: %s", artifact_directory)

    status: dict = {
        "replot": {
            "url": replot_url,
            "download_directory": str(replot_download_dir),
            "output_directory": str(artifact_directory),
            "keep_download_dir": keep_replot_dir,
        }
    }

    try:
        # Step 1: Download artifacts
        _run_download_step(replot_url, replot_download_dir, vault_name, vault_mlflow_secret, status)
        logger.info("Download step completed")

        # Step 2: Replay Prometheus metrics from captured TSDB
        _run_replay_step(artifact_directory, status)

        # Step 3: Run post-processing
        postprocess_success = _run_postprocess_step(artifact_directory, postprocess_config, status)

        # Step 4: Clean up
        if not keep_replot_dir and replot_download_dir.exists():
            logger.info("Cleaning up download directory: %s", replot_download_dir)
            shutil.rmtree(replot_download_dir)
            status["replot"]["cleanup_status"] = "completed"
        elif keep_replot_dir and replot_download_dir.exists():
            logger.info("Keeping download directory as requested: %s", replot_download_dir)
            status["replot"]["cleanup_status"] = "skipped"
        else:
            status["replot"]["cleanup_status"] = "not_needed"

        if postprocess_success:
            status["replot"]["status"] = "success"
            status["replot"]["message"] = "Replot completed successfully"
        else:
            status["replot"]["status"] = "failed"
            status["replot"]["message"] = "Replot completed but postprocessing failed"

    except Exception as e:
        logger.error("Replot failed: %s", e)
        status["replot"]["status"] = "failed"
        status["replot"]["message"] = str(e)

        if not keep_replot_dir and replot_download_dir.exists():
            try:
                shutil.rmtree(replot_download_dir)
                status["replot"]["cleanup_status"] = "completed_on_failure"
            except Exception as cleanup_e:
                logger.warning("Failed to cleanup download directory: %s", cleanup_e)
                status["replot"]["cleanup_status"] = "failed"

        raise

    return status


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run_replot(*, artifact_directory: Path | None, keep_download_dir: bool | None = None):
    if artifact_directory is None:
        artifact_directory = env.ARTIFACT_DIR

    replot_url = config.project.get_config("caliper.replot.url", "", print=False, warn=False)

    if keep_download_dir is not None:
        keep_replot_dir = keep_download_dir
    else:
        keep_replot_dir = config.project.get_config(
            "caliper.replot.keep", False, print=False, warn=False
        )

    vault_name = None
    vault_mlflow_secret = None

    if replot_url:
        export_config = config.project.get_config("caliper.export", {}, print=False, warn=False)
        mlflow_backend = export_config.get("backend", {}).get("mlflow", {})

        if not mlflow_backend.get("enabled", False):
            raise ValueError(
                "MLflow export is not enabled in configuration. Cannot determine authentication settings."
            )

        secrets_config = mlflow_backend.get("secrets", {}).get("vault", {})
        vault_name = secrets_config.get("name")
        vault_mlflow_secret = secrets_config.get("mlflow_secret")

        if not vault_name or not vault_mlflow_secret:
            raise ValueError(
                "MLflow vault configuration missing. Check caliper.export.backend.mlflow.secrets.vault settings."
            )

    postprocess_config = config.project.get_config(
        "caliper.postprocess", {}, print=False, warn=False
    )

    return run_replot_from_orchestration_config(
        replot_url=replot_url,
        artifact_directory=artifact_directory,
        vault_name=vault_name,
        vault_mlflow_secret=vault_mlflow_secret,
        keep_replot_dir=keep_replot_dir,
        postprocess_config=postprocess_config,
    )


@click.command("replot")
@click.option(
    "--artifact-directory",
    "artifact_directory",
    type=click.Path(path_type=Path, exists=False, file_okay=True, dir_okay=True),
    default=None,
    help="Artifact root directory to replot from (defaults to ARTIFACT_DIR).",
)
@click.option(
    "--url",
    "replot_url",
    type=str,
    default=None,
    help="Remote URL to replot artifacts from (if not provided, uses existing config or skips download).",
)
@click.option(
    "--keep-download",
    is_flag=True,
    default=None,
    help="Keep the download directory after processing (don't clean up).",
)
@click.pass_context
@ci_lib.safe_ci_entrypoint
def caliper_replot_entrypoint(
    _ctx, artifact_directory: Path | None, replot_url: str | None, keep_download: bool | None
):
    """Replot artifacts and visualizations from a remote URL."""

    if replot_url is not None:
        config.project.set_config("caliper.replot.url", replot_url)

    status = run_replot(artifact_directory=artifact_directory, keep_download_dir=keep_download)

    import yaml

    logger.info("Replot status:")
    logger.info(yaml.dump(status, indent=2))

    try:
        logger.info(
            "Artifacts saved to: %s",
            status["replot"]["postprocess_result"]["base_directory"],
        )
    except Exception:
        pass

    replot_status = status.get("replot", {}).get("status", "unknown")
    if replot_status != "success":
        return 1, ExitCategory.INTERNAL_ERROR, f"Replot failed with status: {replot_status}"
