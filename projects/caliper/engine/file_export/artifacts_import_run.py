"""
Shared implementation for ``caliper artifacts import`` (CLI and orchestration).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import click

# Will conditionally suppress SSL warnings based on insecure_tls flag
import urllib3

from projects.caliper.engine.file_export.mlflow_secrets import (
    load_mlflow_secrets_yaml,
    mlflow_connection_env,
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
) -> None:
    """Download artifacts from MLflow."""

    try:
        import mlflow
    except ImportError as e:
        raise RuntimeError(
            "mlflow is required for MLflow import. Install with: pip install mlflow"
        ) from e

    # Parse MLflow URL if provided
    if mlflow_url and not mlflow_run_id:
        from projects.caliper.cli.main import parse_mlflow_url

        try:
            parsed = parse_mlflow_url(mlflow_url)
            mlflow_run_id = parsed.get("run_id")
            if not mlflow_tracking_uri:
                mlflow_tracking_uri = parsed.get("tracking_uri")
            if not mlflow_experiment:
                mlflow_experiment = parsed.get("experiment")
            if not mlflow_workspace:
                mlflow_workspace = parsed.get("workspace")
            # If URL contains artifact path, append to the user-specified one
            url_artifact_path = parsed.get("artifact_path", "")
            if url_artifact_path:
                if artifact_path:
                    artifact_path = f"{artifact_path}/{url_artifact_path}"
                else:
                    artifact_path = url_artifact_path

            click.echo("🔍 Parsed MLflow URL:")
            click.echo(f"  📍 Tracking URI: {mlflow_tracking_uri}")
            click.echo(f"  🆔 Run ID: {mlflow_run_id}")
            click.echo(f"  🧪 Experiment: {mlflow_experiment}")
            click.echo(f"  🏢 Workspace: {mlflow_workspace}")
            click.echo(f"  📁 Artifact path: {artifact_path or '(root)'}")
        except Exception as e:
            raise ValueError(f"Failed to parse MLflow URL: {e}") from e

    # Validate workspace is set
    if not mlflow_workspace:
        raise ValueError(
            "MLflow workspace is required but not found. "
            "Make sure the MLflow URL contains workspace information or specify --mlflow-workspace"
        )

    if not mlflow_run_id:
        raise ValueError("Either --from-mlflow or --from-mlflow-url is required")

    if not mlflow_tracking_uri:
        mlflow_tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
        if not mlflow_tracking_uri:
            raise ValueError(
                "MLflow tracking URI is required: --mlflow-tracking-uri, MLFLOW_TRACKING_URI, "
                "or provide a complete --from-mlflow-url"
            )

    # Load secrets if provided or try to load from vault
    connection_config = {}
    click.echo("\n🔐 Authentication Configuration:")

    if mlflow_secrets_path:
        click.echo(f"  📄 Loading secrets from file: {mlflow_secrets_path}")
        try:
            secrets_data = load_mlflow_secrets_yaml(mlflow_secrets_path)
            validate_mlflow_secrets(secrets_data)
            connection_config = secrets_data
            click.echo("  ✅ Successfully loaded secrets from file")
            click.echo(f"  🔑 Available auth methods: {list(secrets_data.keys())}")
        except Exception as e:
            raise ValueError(f"Failed to load MLflow secrets: {e}") from e
    elif mlflow_url:  # Only try vault if using MLflow URL
        click.echo("  🗄️  Trying to load secrets from vault...")
        # Try to load secrets from vault if no explicit secrets file provided
        vault_secrets = _try_load_mlflow_secrets_from_vault()
        if vault_secrets:
            connection_config = vault_secrets
            click.echo("  ✅ Successfully loaded secrets from vault")
            click.echo(f"  🔑 Available auth methods: {list(vault_secrets.keys())}")
        else:
            click.echo("  ⚠️  No vault secrets found, proceeding without authentication")
    else:
        click.echo("  ℹ️  No secrets file specified, proceeding without authentication")

    # Set up insecure TLS if needed
    if mlflow_insecure_tls:
        connection_config["insecure_tls"] = True

    # Check if insecure_tls was loaded from vault secrets and update the flag
    if connection_config.get("insecure_tls", False):
        mlflow_insecure_tls = True
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        click.echo("🔓 SSL warnings suppressed due to vault insecure_tls setting")

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    click.echo("\n📦 MLflow Artifact Download Configuration:")
    click.echo(f"  📍 Tracking URI: {mlflow_tracking_uri}")
    click.echo(f"  🆔 Run ID: {mlflow_run_id}")
    click.echo(f"  📁 Artifact path: {artifact_path or '(root)'}")
    click.echo(f"  📂 Output directory: {output_dir}")
    click.echo(f"  🏢 Workspace: {mlflow_workspace or '(none)'}")
    click.echo(f"  ⏱️  Timeout: {timeout}s")
    click.echo(f"  🔓 Insecure TLS: {mlflow_insecure_tls}")
    if connection_config:
        click.echo(f"  🔐 Authentication: Enabled ({len(connection_config)} config items)")
    else:
        click.echo("  🔓 Authentication: None")

    def _download_artifacts() -> None:
        # Set up MLflow environment
        if mlflow_workspace:
            os.environ["MLFLOW_WORKSPACE"] = mlflow_workspace

        if mlflow_insecure_tls:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                click.echo("🔓 SSL warnings suppressed for MLflow connection")
            except Exception:
                pass

        # Set tracking URI and get client
        click.echo(f"\n🔗 Setting MLflow tracking URI: {mlflow_tracking_uri}")
        mlflow.set_tracking_uri(mlflow_tracking_uri)

        click.echo("🏗️  Creating MLflow client...")
        client = mlflow.tracking.MlflowClient()

        click.echo("🔍 Attempting to connect to MLflow server...")
        if connection_config:
            click.echo("🔐 Using authentication credentials")

        # Verify run exists
        try:
            click.echo(f"📋 Fetching run metadata for ID: {mlflow_run_id}")
            run = client.get_run(mlflow_run_id)
            click.echo("✅ Successfully connected to MLflow!")
            click.echo(f"🏃 Found run: {run.info.run_name or '(unnamed)'}")
            click.echo(f"📊 Status: {run.info.status}")
            click.echo(f"🧪 Experiment ID: {run.info.experiment_id}")
            click.echo(f"🕐 Start time: {run.info.start_time}")
            click.echo(f"📈 Lifecycle stage: {run.info.lifecycle_stage}")
        except Exception as e:
            error_msg = str(e)
            click.echo("❌ Failed to access MLflow run!")
            click.echo(f"🔍 Error details: {error_msg}")
            click.echo("💡 Troubleshooting hints:")
            if "RESOURCE_DOES_NOT_EXIST" in error_msg:
                click.echo(
                    "   • This often indicates authentication issues rather than missing run"
                )
                click.echo("   • Check if MLflow credentials are properly configured")
                click.echo("   • Verify the run ID is correct in the URL")
            if "ssl" in error_msg.lower() or "certificate" in error_msg.lower():
                click.echo(
                    "   • Try adding --mlflow-insecure-tls flag for self-signed certificates"
                )
            if not connection_config:
                click.echo(
                    "   • No authentication credentials found - this server may require authentication"
                )
                click.echo(
                    f"   • Check vault '{DEFAULT_MLFLOW_VAULT_NAME}' for '{DEFAULT_MLFLOW_SECRET_KEY}'"
                )
            raise ValueError(f"Failed to access MLflow run {mlflow_run_id}: {e}") from e

        # Download artifacts to a temporary directory first
        import tempfile

        with tempfile.TemporaryDirectory(prefix="caliper_mlflow_import_") as temp_dir:
            temp_path = Path(temp_dir)

            click.echo(f"📥 Downloading artifacts to temporary directory: {temp_path}")

            try:
                # Download artifacts
                artifact_uri = client.download_artifacts(
                    run_id=mlflow_run_id, path=artifact_path or "", dst_path=str(temp_path)
                )

                click.echo(f"📦 Downloaded artifacts to: {artifact_uri}")

                # Find what was actually downloaded
                downloaded_path = Path(artifact_uri)
                if not downloaded_path.exists():
                    raise RuntimeError(
                        f"Download completed but path does not exist: {downloaded_path}"
                    )

                # Count files
                if downloaded_path.is_file():
                    downloaded_files = [downloaded_path]
                else:
                    downloaded_files = list(downloaded_path.rglob("*"))
                    downloaded_files = [f for f in downloaded_files if f.is_file()]

                if not downloaded_files:
                    click.echo("Warning: No files were downloaded")
                    return

                # Move files from temp directory to final output directory
                click.echo(f"📁 Moving {len(downloaded_files)} file(s) to output directory...")
                if downloaded_path.is_file():
                    # Single file download
                    target_file = output_dir / downloaded_path.name
                    shutil.move(str(downloaded_path), str(target_file))
                    click.echo(f"📄 Moved file: {target_file.name}")
                else:
                    # Directory download - move contents
                    for file_path in downloaded_files:
                        # Calculate relative path from download root
                        try:
                            rel_path = file_path.relative_to(downloaded_path)
                        except ValueError:
                            # Fallback if paths don't match
                            rel_path = file_path.name

                        target_file = output_dir / rel_path
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(file_path), str(target_file))

                        click.echo(f"📄 Moved file: {rel_path}")

                # Final count of moved files
                final_files = list(output_dir.rglob("*"))
                final_files = [f for f in final_files if f.is_file()]

                click.echo(f"✅ Successfully downloaded {len(final_files)} file(s) to {output_dir}")

                if final_files:
                    click.echo("📋 Downloaded files:")
                    for file_path in sorted(final_files):
                        try:
                            rel_path = file_path.relative_to(output_dir)
                            click.echo(f"  {rel_path}")
                        except ValueError:
                            click.echo(f"  {file_path}")

            except Exception as e:
                raise RuntimeError(f"Failed to download artifacts from MLflow: {e}") from e

    # Execute download with proper connection context
    if connection_config:
        with mlflow_connection_env(connection_config):
            _download_artifacts()
    else:
        _download_artifacts()


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
        secrets_data = load_mlflow_secrets_yaml(secret_path)
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
