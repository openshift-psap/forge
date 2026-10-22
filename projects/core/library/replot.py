"""
Shared "replot" CLI for FORGE project orchestration.

Registers a click subcommand for replotting artifacts and visualizations.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from projects.caliper.orchestration.replot import run_replot_from_orchestration_config
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.core.library.ci import ExitCategory

logger = logging.getLogger(__name__)


def run_replot(*, artifact_directory: Path | None, keep_download_dir: bool | None = None):
    """Run replotting logic on the specified artifact directory."""

    if artifact_directory is None:
        artifact_directory = env.ARTIFACT_DIR

    # Get the replot URL from configuration (empty string if not configured)
    replot_url = config.project.get_config("caliper.replot.url", "", print=False, warn=False)

    # Determine keep setting: CLI flag > config > default False
    if keep_download_dir is not None:
        keep_replot_dir = keep_download_dir
    else:
        keep_replot_dir = config.project.get_config(
            "caliper.replot.keep", False, print=False, warn=False
        )

    # Load MLflow configuration from export settings to get vault secrets (only if URL provided)
    vault_name = None
    vault_mlflow_secret = None

    if replot_url:  # Only load MLflow config if we have a URL to download from
        export_config = config.project.get_config("caliper.export", {}, print=False, warn=False)
        mlflow_backend = export_config.get("backend", {}).get("mlflow", {})

        if not mlflow_backend.get("enabled", False):
            raise ValueError(
                "MLflow export is not enabled in configuration. Cannot determine authentication settings."
            )

        # Get vault secrets configuration (same as export)
        secrets_config = mlflow_backend.get("secrets", {}).get("vault", {})
        vault_name = secrets_config.get("name")
        vault_mlflow_secret = secrets_config.get("mlflow_secret")

        if not vault_name or not vault_mlflow_secret:
            raise ValueError(
                "MLflow vault configuration missing. Check caliper.export.backend.mlflow.secrets.vault settings."
            )

    # Get post-processing configuration
    postprocess_config = config.project.get_config(
        "caliper.postprocess", {}, print=False, warn=False
    )

    # Run the actual replot operation
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

    # Only override config if URL was explicitly provided via CLI
    if replot_url is not None:
        config.project.set_config("caliper.replot.url", replot_url)

    status = run_replot(artifact_directory=artifact_directory, keep_download_dir=keep_download)

    # Log the status
    import yaml

    logger.info("Replot status:")
    logger.info(yaml.dump(status, indent=2))

    try:
        logger.info(
            f"📁 Artifacts saved to: {status['replot']['postprocess_result']['base_directory']}"
        )
    except Exception:
        pass

    replot_status = status.get("replot", {}).get("status", "unknown")
    if replot_status != "success":
        return 1, ExitCategory.INTERNAL_ERROR, f"Replot failed with status: {replot_status}"
