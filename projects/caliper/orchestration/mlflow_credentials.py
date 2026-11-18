"""Load the MLflow connection selected by Forge project configuration."""

from pathlib import Path
from typing import Any

from projects.caliper.public.mlflow import load_mlflow_connection_file, validate_mlflow_secrets
from projects.core.library import config, vault

INSTANCE_CONFIG_PATH = "caliper.export.backend.mlflow.instance"
INSTANCE_SECRET_KEY = "mlflow-instance-secrets.yaml"


def _resolve_mlflow_credentials(vault_name: str, default_key: str) -> tuple[Path, str | None]:
    """Keep the existing file unless the project explicitly selects a profile."""
    instance = (
        config.project.get_config(INSTANCE_CONFIG_PATH, None, warn=False, print=False)
        if config.project is not None
        else None
    )
    if instance is not None and (not isinstance(instance, str) or not instance.strip()):
        raise ValueError(f"{INSTANCE_CONFIG_PATH} must be a non-empty profile name")

    key = INSTANCE_SECRET_KEY if instance is not None else default_key
    path = vault.get_vault_content_path(vault_name, key)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"MLflow credentials missing from vault {vault_name}/{key}")
    return path, instance


def load_mlflow_connection(vault_name: str, default_key: str) -> dict[str, Any]:
    """Load and validate credentials for the configured MLflow instance."""
    path, instance = _resolve_mlflow_credentials(vault_name, default_key)
    connection = load_mlflow_connection_file(path, mlflow_instance=instance)
    validate_mlflow_secrets(connection)
    return connection
