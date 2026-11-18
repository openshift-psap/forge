"""
Shared implementation for ``caliper artifacts import`` (CLI and orchestration).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import warnings
from pathlib import Path
from typing import Any

import click
import urllib3

from projects.caliper.engine.mlflow_connection import (
    load_mlflow_connection_file,
    mlflow_tracking_context,
    parse_mlflow_url,
    resolve_mlflow_tracking_uri,
    validate_mlflow_secrets,
)

logger = logging.getLogger(__name__)

# Default vault configuration for MLflow secrets
DEFAULT_MLFLOW_VAULT_NAME = "psap-forge-mlflow-export"
DEFAULT_MLFLOW_SECRET_KEY = "mlflow-secret.yaml"


def run_artifacts_import(
    *,
    mlflow_run_id: str | None = None,
    mlflow_url: str | None = None,
    output_dir: Path,
    mlflow_tracking_uri: str | None = None,
    artifact_path: str = "",
    timeout: int = 300,
    mlflow_insecure_tls: bool = False,
    mlflow_experiment: str | None = None,
    mlflow_workspace: str | None = None,
    mlflow_secrets_path: Path | None = None,
    mlflow_instance: str | None = None,
) -> None:
    """Resolve standalone CLI inputs and call the shared downloader."""
    if mlflow_url and not mlflow_run_id:
        parsed = parse_mlflow_url(mlflow_url)
        mlflow_run_id = parsed.get("run_id")
        mlflow_tracking_uri = mlflow_tracking_uri or parsed.get("tracking_uri")
        mlflow_workspace = mlflow_workspace or parsed.get("workspace")
        artifact_path = "/".join(filter(None, (artifact_path, parsed.get("artifact_path"))))

    if not mlflow_run_id:
        raise ValueError("Either --from-mlflow or --from-mlflow-url is required")
    if not mlflow_workspace:
        raise ValueError("Specify --mlflow-workspace or include a workspace in the MLflow URL")
    if mlflow_instance is not None and mlflow_secrets_path is None:
        raise ValueError("--mlflow-instance requires --mlflow-secrets")

    connection = None
    if mlflow_secrets_path:
        connection = load_mlflow_connection_file(
            mlflow_secrets_path, mlflow_instance=mlflow_instance
        )
    elif mlflow_url:
        connection = _try_load_mlflow_secrets_from_vault()

    if connection is None:
        # Preserve standalone imports authenticated through the MLflow environment.
        connection = {
            key: value
            for key in ("token", "username", "password", "server_cert_path")
            if (value := os.environ.get(f"MLFLOW_TRACKING_{key.upper()}"))
        }
        connection["insecure_tls"] = (
            os.environ.get("MLFLOW_TRACKING_INSECURE_TLS", "").lower() == "true"
        )

    uri = resolve_mlflow_tracking_uri(connection, mlflow_tracking_uri)
    if not uri:
        uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        raise ValueError("MLflow tracking URI is required")
    if mlflow_insecure_tls:
        connection["insecure_tls"] = True

    downloaded = download_mlflow_artifacts(
        connection={**connection, "tracking_uri": uri},
        run_id=mlflow_run_id,
        workspace=mlflow_workspace,
        artifact_path=artifact_path,
        output_dir=output_dir,
    )
    click.echo(f"Downloaded {downloaded} file(s) to {output_dir}")


def download_mlflow_artifacts(
    *,
    connection: dict[str, Any],
    run_id: str,
    workspace: str,
    artifact_path: str,
    output_dir: Path,
) -> int:
    """Download a run's files using a resolved connection."""
    try:
        import mlflow
    except ImportError as e:
        raise RuntimeError(
            "mlflow is required for MLflow import. Install with: pip install mlflow"
        ) from e

    validate_mlflow_secrets(connection)
    tracking_uri = resolve_mlflow_tracking_uri(connection)
    if not tracking_uri:
        raise ValueError("The MLflow connection needs a tracking URI")
    output_dir.mkdir(parents=True, exist_ok=True)

    with (
        mlflow_tracking_context(connection, workspace=workspace),
        warnings.catch_warnings(),
        tempfile.TemporaryDirectory(prefix="caliper_mlflow_import_") as temp_dir,
    ):
        if connection.get("insecure_tls"):
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        client = mlflow.tracking.MlflowClient(tracking_uri=tracking_uri)
        client.get_run(run_id)
        downloaded_path = Path(
            client.download_artifacts(run_id=run_id, path=artifact_path, dst_path=temp_dir)
        )
        if not downloaded_path.exists():
            raise RuntimeError("MLflow download did not produce an artifact path")

        if downloaded_path.is_file():
            files = [downloaded_path]
            root = downloaded_path.parent
        else:
            files = [path for path in downloaded_path.rglob("*") if path.is_file()]
            root = downloaded_path

        for path in files:
            destination = output_dir / path.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), destination)
        return len(files)


def _try_load_mlflow_secrets_from_vault() -> dict | None:
    """Try to load MLflow secrets from vault using environment variables or defaults.

    This also initializes all caliper-related vaults for consistency with orchestration runs.

    Returns:
        MLflow secrets dictionary if found, None otherwise
    """
    import os

    # Get vault name and secret key from environment or use defaults
    mlflow_vault_name = os.environ.get(
        "PSAP_FORGE_MLFLOW_EXPORT_SECRET_VAULT", DEFAULT_MLFLOW_VAULT_NAME
    )
    secret_key = os.environ.get("PSAP_FORGE_MLFLOW_EXPORT_SECRET_KEY", DEFAULT_MLFLOW_SECRET_KEY)

    try:
        from projects.core.library import vault as vault_lib

        click.echo("  🏗️  Initializing caliper vault system...")

        # Use the same vault listing functions as orchestration runs
        try:
            from projects.core.library.export import (
                caliper_export_list_optional_vaults,
                caliper_export_list_vaults,
            )

            # Get mandatory caliper vaults (S3, MLflow export, etc.)
            mandatory_vaults = caliper_export_list_vaults()
            click.echo(f"  📊 Found mandatory vaults: {mandatory_vaults}")

            # Get optional caliper vaults (notifications, etc.)
            optional_vaults = caliper_export_list_optional_vaults()
            if optional_vaults:
                click.echo(f"  📧 Found optional vaults: {optional_vaults}")

            # Always include the specific MLflow vault we need for import
            if mlflow_vault_name not in mandatory_vaults:
                mandatory_vaults.append(mlflow_vault_name)
                click.echo(f"  🔍 Added MLflow import vault: {mlflow_vault_name}")

            # Initialize using the same pattern as orchestration
            vault_lib.init(mandatory_vaults=mandatory_vaults, optional_vaults=optional_vaults)
            click.echo(
                f"  ✅ Initialized {len(mandatory_vaults)} mandatory + {len(optional_vaults)} optional vaults"
            )

        except Exception as e:
            # Fallback to just the MLflow vault if vault listing fails
            click.echo(f"  ⚠️  Vault listing failed: {e}")
            click.echo(f"  🔍 Using fallback vault: {mlflow_vault_name}")
            vault_lib.init(vaults=[mlflow_vault_name])

        # Now try to get the MLflow secrets specifically
        click.echo(
            f"  🔍 Looking for MLflow secret '{secret_key}' in vault '{mlflow_vault_name}'..."
        )
        secret_path = vault_lib.get_vault_content_path(mlflow_vault_name, secret_key)

        if secret_path is None:
            click.echo(f"  ❌ Secret '{secret_key}' not found in vault '{mlflow_vault_name}'")
            return None

        if not secret_path.exists():
            click.echo(f"  ❌ Secret file path exists but file missing: {secret_path}")
            return None

        click.echo(f"  ✅ Found MLflow secrets file: {secret_path}")
        click.echo("  📄 Loading and validating secrets...")

        # Load and validate the secrets
        secrets_data = load_mlflow_connection_file(secret_path)
        validate_mlflow_secrets(secrets_data)

        click.echo("  ✅ Successfully validated MLflow secrets from vault")
        # Don't print actual secret contents, just structure
        secret_keys = list(secrets_data.keys())
        click.echo(f"  🔑 Secret contains: {', '.join(secret_keys)}")

        return secrets_data

    except ImportError:
        click.echo("  ⚠️  Vault library not available, skipping vault secret lookup")
        return None
    except Exception as e:
        click.echo(f"  ❌ Failed to load secrets from vault: {e}")
        return None
