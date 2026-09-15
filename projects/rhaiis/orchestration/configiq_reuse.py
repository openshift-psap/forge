"""Resolve and download a prior ConfigIQ Tier 1 report from MLflow."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from projects.caliper.engine.file_export.artifacts_import_run import run_artifacts_import
from projects.caliper.engine.file_export.mlflow_secrets import (
    load_mlflow_secrets_yaml,
    mlflow_connection_env,
    validate_mlflow_secrets,
)
from projects.core.library import vault
from projects.core.library.config import requires

logger = logging.getLogger(__name__)


@requires(
    experiment="caliper.export.backend.mlflow.config.experiment",
    workspace="caliper.export.backend.mlflow.config.workspace",
    vault_name="caliper.export.backend.mlflow.secrets.vault.name",
    vault_key="caliper.export.backend.mlflow.secrets.vault.mlflow_secret",
)
def download_tier1_report_by_uuid(
    *,
    run_uuid: str,
    output_dir: Path,
    _cfg=None,
) -> str:
    """Download the unique ConfigIQ Tier 1 child run matching a FORGE UUID.

    Returns the resolved MLflow child run ID. Authentication is read from the
    existing Caliper MLflow vault and is never written to the artifact tree.
    """
    if not _cfg.experiment:
        raise ValueError("ConfigIQ Tier 1 reuse requires an MLflow experiment")
    if not _cfg.workspace:
        raise ValueError("ConfigIQ Tier 1 reuse requires an MLflow workspace")
    secret_path = vault.get_vault_content_path(_cfg.vault_name, _cfg.vault_key)
    if secret_path is None or not secret_path.exists():
        raise FileNotFoundError(
            f"MLflow secret file {_cfg.vault_name}/{_cfg.vault_key} was not found"
        )

    connection = load_mlflow_secrets_yaml(secret_path)
    validate_mlflow_secrets(connection)
    tracking_uri = connection.get("tracking_uri")
    if not tracking_uri:
        raise ValueError("The MLflow vault configuration must contain a tracking_uri")

    import mlflow

    previous_workspace = os.environ.get("MLFLOW_WORKSPACE")
    previous_tracking_uri = mlflow.get_tracking_uri()
    try:
        with mlflow_connection_env(connection):
            os.environ["MLFLOW_WORKSPACE"] = _cfg.workspace
            mlflow.set_tracking_uri(tracking_uri)
            client = mlflow.tracking.MlflowClient()
            experiment = client.get_experiment_by_name(_cfg.experiment)
            if experiment is None:
                raise ValueError(f"MLflow experiment {_cfg.experiment!r} was not found")

            runs = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                filter_string=(
                    f"params.run_uuid = '{run_uuid}' AND params.configiq_pass = 'tier1'"
                ),
                max_results=2,
            )
            if not runs:
                raise FileNotFoundError(
                    f"No ConfigIQ Tier 1 MLflow run found for run UUID {run_uuid}"
                )
            if len(runs) > 1:
                raise RuntimeError(
                    f"Multiple ConfigIQ Tier 1 MLflow runs found for run UUID {run_uuid}"
                )

            mlflow_run_id = runs[0].info.run_id
            logger.info(
                "Resolved ConfigIQ run UUID %s to MLflow Tier 1 run %s",
                run_uuid,
                mlflow_run_id,
            )
            run_artifacts_import(
                mlflow_run_id=mlflow_run_id,
                output_dir=output_dir,
                mlflow_tracking_uri=tracking_uri,
                mlflow_workspace=_cfg.workspace,
                mlflow_secrets_path=secret_path,
            )
    finally:
        if previous_workspace is None:
            os.environ.pop("MLFLOW_WORKSPACE", None)
        else:
            os.environ["MLFLOW_WORKSPACE"] = previous_workspace
        mlflow.set_tracking_uri(previous_tracking_uri)

    return mlflow_run_id
