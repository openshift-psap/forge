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
from projects.core.library import config, env, vault
from projects.core.library.export import (
    caliper_agentic_list_vaults,
    caliper_export_list_optional_vaults,
    caliper_export_list_vaults,
)

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

    # Initialize vaults needed for replot operations
    logger.info("Initializing vaults for replot operations")
    try:
        # Collect all required vaults using the same conditional logic as postprocess
        all_vaults = []

        # Add general project vaults
        phase_vaults = vault.phase_vault_list_all()
        all_vaults.extend(phase_vaults)
        logger.info(f"Added {len(phase_vaults)} phase vaults: {phase_vaults}")

        # Add export-specific vaults (MLflow, S3, notifications)
        export_vaults = caliper_export_list_vaults()
        all_vaults.extend(export_vaults)
        logger.info(f"Added {len(export_vaults)} export vaults: {export_vaults}")

        # Add agentic vaults (models)
        agentic_vaults = caliper_agentic_list_vaults()
        all_vaults.extend(agentic_vaults)
        logger.info(f"Added {len(agentic_vaults)} agentic vaults: {agentic_vaults}")

        # Add optional export vaults (notifications)
        optional_export_vaults = caliper_export_list_optional_vaults()
        logger.info(
            f"Found {len(optional_export_vaults)} optional export vaults: {optional_export_vaults}"
        )

        # Remove duplicates while preserving order
        unique_vaults = list(dict.fromkeys(all_vaults))
        unique_optional_vaults = list(dict.fromkeys(optional_export_vaults))

        logger.info(f"Total required vaults for replot: {len(unique_vaults)} - {unique_vaults}")
        logger.info(
            f"Total optional vaults for replot: {len(unique_optional_vaults)} - {unique_optional_vaults}"
        )

        # Initialize vaults with optional vaults handled separately
        if unique_vaults or unique_optional_vaults:
            vault.init(vaults=unique_vaults, optional_vaults=unique_optional_vaults)
            logger.info(
                f"Successfully initialized {len(unique_vaults)} required + {len(unique_optional_vaults)} optional vaults for replot"
            )
        else:
            logger.info("No vaults needed for replot operation")

    except Exception as e:
        logger.warning(f"Failed to initialize vaults for replot: {e}")
        logger.warning("Continuing with replot operation - some features may not work")

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
    help="Keep the download directory after processing (don't clean up).",
)
@click.pass_context
@ci_lib.safe_ci_entrypoint
def caliper_replot_entrypoint(
    _ctx, artifact_directory: Path | None, replot_url: str | None, keep_download: bool
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

    # Show where artifacts have been saved
    final_artifact_dir = artifact_directory or env.ARTIFACT_DIR
    logger.info(f"📁 Artifacts saved to: {final_artifact_dir}")

    # Check if replot was successful
    replot_status = status.get("replot", {}).get("status", "unknown")
    if replot_status != "success":
        return 1

    return 0
