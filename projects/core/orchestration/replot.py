"""
Consolidated replot orchestration for FORGE projects.

Handles MLflow artifact downloading, Prometheus TSDB replay, and
post-processing pipeline execution.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import click

from projects.caliper.orchestration.mlflow_credentials import load_mlflow_connection
from projects.caliper.orchestration.postprocess import run_postprocess_from_orchestration_config
from projects.caliper.orchestration.postprocess_outcome import TestPhaseOutcome
from projects.caliper.public.mlflow import (
    download_mlflow_artifacts,
    parse_mlflow_url,
    resolve_mlflow_tracking_uri,
)
from projects.cluster.library.prom.replay import replay_metrics_from_config
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
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

    if not vault_name or not vault_mlflow_secret:
        raise ValueError("MLflow vault configuration is missing")
    destination = parse_mlflow_url(replot_url)
    run_id = destination["run_id"]
    workspace = destination["workspace"]
    if not run_id or not workspace:
        raise ValueError("The replot URL must contain an MLflow run ID and workspace")

    connection = load_mlflow_connection(vault_name, vault_mlflow_secret)
    resolve_mlflow_tracking_uri(connection, destination["tracking_uri"])
    downloaded = download_mlflow_artifacts(
        connection=connection,
        run_id=run_id,
        workspace=workspace,
        artifact_path=destination["artifact_path"] or "",
        output_dir=replot_download_dir,
    )
    if not downloaded:
        raise RuntimeError(f"No MLflow artifacts downloaded to {replot_download_dir}")

    status["replot"].update(
        {"download_status": "success", "downloaded_files": downloaded, "run_id": run_id}
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

        allow_refresh = config.project.get_config(
            "caliper.replot.prom.metrics.allow_refresh", False, print=False, warn=False
        )
        logger.info("Checking for Prometheus metrics to replay...")
        replay_errors = replay_metrics_from_config(artifact_directory, force_refresh=allow_refresh)

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
