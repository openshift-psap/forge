"""S3 import functionality for Caliper historical data."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

try:
    import boto3
    import botocore.session
    from botocore.exceptions import BotoCoreError, ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None
    botocore = None
    BotoCoreError = None
    ClientError = None
    BOTO3_AVAILABLE = False

from projects.core.library import vault as vault_lib

logger = logging.getLogger(__name__)


def list_s3_objects(s3_client, bucket: str, prefix: str, max_objects: int = 1000) -> list[dict]:
    """List objects in S3 bucket with given prefix.

    Args:
        s3_client: Configured boto3 S3 client
        bucket: S3 bucket name
        prefix: S3 key prefix to filter objects
        max_objects: Maximum number of objects to return

    Returns:
        List of S3 object metadata dictionaries
    """
    try:
        objects = []
        paginator = s3_client.get_paginator("list_objects_v2")

        for page in paginator.paginate(
            Bucket=bucket, Prefix=prefix, PaginationConfig={"MaxItems": max_objects}
        ):
            if "Contents" in page:
                for obj in page["Contents"]:
                    objects.append(
                        {
                            "key": obj["Key"],
                            "size": obj["Size"],
                            "last_modified": obj["LastModified"],
                        }
                    )

        logger.info(f"Found {len(objects)} objects with prefix: {prefix}")
        return objects

    except (ClientError, BotoCoreError) as e:
        logger.error(f"Failed to list S3 objects: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error listing S3 objects: {e}")
        raise


def download_file_from_s3(s3_client, bucket: str, s3_key: str, local_path: Path) -> bool:
    """Download a single file from S3.

    Args:
        s3_client: Configured boto3 S3 client
        bucket: S3 bucket name
        s3_key: S3 object key
        local_path: Local file path to save to

    Returns:
        True if download succeeded, False otherwise
    """
    try:
        # Create parent directories if needed
        local_path.parent.mkdir(parents=True, exist_ok=True)

        logger.debug(f"Downloading s3://{bucket}/{s3_key} to {local_path}")
        s3_client.download_file(bucket, s3_key, str(local_path))
        logger.debug(
            f"Successfully downloaded {local_path.name} ({local_path.stat().st_size} bytes)"
        )
        return True

    except (ClientError, BotoCoreError) as e:
        logger.error(f"Failed to download {s3_key}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error downloading {s3_key}: {e}")
        return False


def create_s3_client(credentials_path: Path) -> Any:
    """Create boto3 S3 client using AWS credentials file.

    Args:
        credentials_path: Path to AWS credentials file

    Returns:
        Configured boto3 S3 client
    """
    if not BOTO3_AVAILABLE:
        raise ImportError("boto3 is not available. Install it with: pip install boto3")

    try:
        import configparser

        # Parse the AWS credentials file
        config = configparser.ConfigParser()
        config.read(credentials_path)

        # Get credentials from [default] section
        if "default" not in config.sections():
            raise ValueError(f"No [default] section found in credentials file: {credentials_path}")

        aws_access_key_id = config["default"].get("aws_access_key_id")
        aws_secret_access_key = config["default"].get("aws_secret_access_key")
        aws_session_token = config["default"].get("aws_session_token")  # Optional

        if not aws_access_key_id or not aws_secret_access_key:
            raise ValueError(
                f"Missing aws_access_key_id or aws_secret_access_key in credentials file: {credentials_path}"
            )

        # Create boto3 session with explicit credentials
        boto_session = boto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,  # Will be None if not present
        )
        s3_client = boto_session.client("s3")

        logger.info("S3 client created with provided credentials")

        return s3_client
    except (ClientError, BotoCoreError) as e:
        logger.error(f"Failed to authenticate with AWS S3: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error creating S3 client: {e}")
        raise


def get_aws_credentials(vault_name: str, credentials_file: str) -> Path | None:
    """Get AWS credentials file from vault.

    Args:
        vault_name: Name of the vault containing AWS credentials
        credentials_file: Name of credentials file within vault

    Returns:
        Path to credentials file or None if not found
    """

    credentials_path = vault_lib.get_vault_content_path(vault_name, credentials_file)

    if credentials_path is None:
        logger.error(f"Vault {vault_name}/{credentials_file} not found")
        return None

    if not credentials_path.exists():
        logger.error(f"Vault credentials file missing: {credentials_path}")
        return None

    logger.info(f"Found AWS credentials at: {credentials_path}")

    return credentials_path


def filter_objects_for_download(objects: list[dict], s3_config) -> list[dict]:
    """Filter S3 objects based on import configuration.

    Args:
        objects: List of S3 object metadata
        s3_config: S3 import configuration

    Returns:
        List of filtered objects to download
    """
    filtered_objects = []

    for obj in objects:
        key = obj["key"]

        # Check if we should download this object based on filters
        should_download = False

        if s3_config.include_kpis_json and key.endswith("kpis.json"):
            should_download = True
        elif s3_config.include_kpis_csv and key.endswith("kpis.csv"):
            should_download = True
        elif s3_config.include_ai_data and "/ai_data/" in key:
            should_download = True

        if should_download:
            filtered_objects.append(obj)

    logger.info(f"Filtered to {len(filtered_objects)} objects for download")
    return filtered_objects


def run_s3_import_with_explicit_params(
    *,
    bucket: str,
    prefix: str,
    output_dir: Path,
    vault: str,
    aws_credentials_file: str,
    include_kpis_json: bool = True,
    include_kpis_csv: bool = False,
    include_ai_data: bool = False,
    max_downloads: int = 50,
) -> dict[str, Any]:
    """S3 import functionality for historical data with explicit parameters.

    Args:
        bucket: S3 bucket name
        prefix: S3 prefix path (full path including s3.prefix/instance/directory)
        output_dir: Path to output directory for downloaded files
        vault: Vault name containing AWS credentials
        aws_credentials_file: AWS credentials file within vault
        include_kpis_json: Whether to include KPI JSON files
        include_kpis_csv: Whether to include KPI CSV files
        include_ai_data: Whether to include AI data files
        max_downloads: Maximum number of files to download

    Returns:
        Import status dictionary
    """
    start_time = time.time()

    if not BOTO3_AVAILABLE:
        return {
            "status": "failed",
            "error": "boto3 is not available. Install it with: pip install boto3",
            "completed_at": time.time(),
        }

    try:
        # Ensure prefix ends with '/' for S3 operations
        import_s3_prefix = prefix.rstrip("/") + "/" if prefix else ""

        logger.info(f"Starting S3 import from bucket: {bucket}")
        logger.info(f"S3 import path: s3://{bucket}/{import_s3_prefix}")

        # Get AWS credentials for download
        credentials_path = get_aws_credentials(vault, aws_credentials_file)
        if not credentials_path:
            return {
                "status": "failed",
                "error": f"Could not load AWS credentials from vault {vault}",
                "completed_at": time.time(),
            }

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Create S3 client
        try:
            s3_client = create_s3_client(credentials_path)
        except Exception as e:
            logger.error(f"Failed to create S3 client: {e}")
            return {
                "status": "failed",
                "error": f"Could not create S3 client: {e}",
                "completed_at": time.time(),
            }

        # List S3 objects (even in dry run mode to show what would be downloaded)
        objects = list_s3_objects(s3_client, bucket, import_s3_prefix, max_downloads)

        logger.info(f"Found {len(objects)} total objects in s3://{bucket}/{import_s3_prefix}")
        if objects:
            logger.info("Sample object keys:")
            for i, obj in enumerate(objects[:5]):  # Show first 5 objects
                logger.info(f"  {i + 1}: {obj['key']}")
            if len(objects) > 5:
                logger.info(f"  ... and {len(objects) - 5} more objects")

        # Filter objects based on parameters
        logger.info(
            f"Filtering with: include_kpis_json={include_kpis_json}, "
            f"include_kpis_csv={include_kpis_csv}, include_ai_data={include_ai_data}"
        )

        # Create filter config object for the filter function
        class FilterConfig:
            def __init__(self):
                self.include_kpis_json = include_kpis_json
                self.include_kpis_csv = include_kpis_csv
                self.include_ai_data = include_ai_data

        download_objects = filter_objects_for_download(objects, FilterConfig())
        logger.info(f"After filtering: {len(download_objects)} objects match the import filters")

        if not download_objects:
            logger.warning("No objects match the import filters")
            return {
                "status": "warning",
                "message": f"no KPI files found matching filters in s3://{bucket}/{import_s3_prefix}",
                "completed_at": time.time(),
            }

        # Show what would be downloaded
        logger.info(f"Objects to be downloaded ({len(download_objects)} total):")
        download_plan = []

        for obj in download_objects:
            s3_key = obj["key"]

            # Determine local path preserving relative structure
            # Remove the base prefix to get the relative path
            if s3_key.startswith(import_s3_prefix):
                relative_key = s3_key[len(import_s3_prefix) :]
            else:
                relative_key = s3_key

            local_path = output_dir / relative_key

            download_plan.append(
                {
                    "s3_key": s3_key,
                    "local_path": str(local_path),
                    "size": obj["size"],
                }
            )
            logger.info(f"  - s3://{bucket}/{s3_key} → {local_path} ({obj['size']} bytes)")

        # Download files from S3
        downloaded_count = 0
        failed_downloads = []

        logger.debug("Starting actual S3 download...")

        for download_item in download_plan:
            s3_key = download_item["s3_key"]
            local_path = Path(download_item["local_path"])

            if download_file_from_s3(s3_client, bucket, s3_key, local_path):
                downloaded_count += 1
            else:
                failed_downloads.append(s3_key)

        # Check download results
        if failed_downloads:
            logger.error(f"Failed to download {len(failed_downloads)} files: {failed_downloads}")
            if downloaded_count == 0:
                return {
                    "status": "failed",
                    "error": f"All downloads failed. Failed files: {failed_downloads}",
                    "completed_at": time.time(),
                }
            else:
                logger.warning(
                    f"Partial success: {downloaded_count} downloaded, {len(failed_downloads)} failed"
                )

        target_url = f"s3://{bucket}/{import_s3_prefix}"
        logger.info(
            f"S3 import completed: {downloaded_count}/{len(download_objects)} files downloaded successfully from {target_url}"
        )

        # Determine final status and message
        message = None
        if failed_downloads:
            status = "partial_success" if downloaded_count > 0 else "failed"
        elif downloaded_count == 0:
            status = "warning"
            message = f"no files downloaded from s3://{bucket}/{import_s3_prefix}"
        else:
            status = "success"

        result = {
            "status": status,
            "bucket": bucket,
            "import_s3_prefix": import_s3_prefix,
            "import_path": f"s3://{bucket}/{import_s3_prefix}",
            "output_dir": str(output_dir),
            "downloaded_files": downloaded_count,
            "failed_files": len(failed_downloads),
            "total_files": len(download_objects),
            "total_size": sum(obj["size"] for obj in download_objects),
            "completed_at": time.time(),
            "duration": round(time.time() - start_time),
        }

        # Add message if there is one
        if message:
            result["message"] = message

        return result

    except Exception as e:
        logger.exception("S3 import failed")
        return {
            "status": "failed",
            "error": str(e),
            "exception_type": type(e).__name__,
            "completed_at": time.time(),
        }
