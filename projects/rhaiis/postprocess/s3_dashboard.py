"""S3 dashboard CSV sync and profiler trace upload for rhaiis."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def sync_csv_to_s3(
    csv_path: Path,
    *,
    s3_bucket: str,
    s3_key: str,
    vault_name: str,
    credentials_file: str = "aws.credentials",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Download consolidated CSV from S3, append new rows, re-upload."""
    try:
        import pandas as pd
    except ImportError:
        return {"status": "failed", "error": "pandas not available"}

    from projects.caliper.cli.s3_export import create_s3_client, get_aws_credentials

    if dry_run:
        logger.info("DRY RUN: Would sync %s to s3://%s/%s", csv_path, s3_bucket, s3_key)
        return {"status": "success", "dry_run": True}

    credentials_path = get_aws_credentials(vault_name, credentials_file)
    if not credentials_path:
        return {"status": "failed", "error": f"AWS credentials not found in vault {vault_name}"}

    consolidated_path = None
    try:
        s3 = create_s3_client(credentials_path)

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            consolidated_path = tmp.name

        try:
            s3.download_file(s3_bucket, s3_key, consolidated_path)
            logger.info("Downloaded s3://%s/%s", s3_bucket, s3_key)
        except Exception as e:
            if hasattr(e, "response") and e.response.get("Error", {}).get("Code") == "404":
                logger.warning("Consolidated CSV not found on S3, uploading new CSV")
                s3.upload_file(str(csv_path), s3_bucket, s3_key)
                return {"status": "success", "action": "created", "rows_added": -1}
            raise

        new_df = pd.read_csv(csv_path)
        if new_df.empty:
            logger.warning("No data rows in generated CSV %s", csv_path)
            return {"status": "skipped", "reason": "empty CSV"}

        existing_df = pd.read_csv(consolidated_path, on_bad_lines="warn")
        merged_df = pd.concat([existing_df, new_df], ignore_index=True)
        merged_df.to_csv(consolidated_path, index=False)
        rows_added = len(new_df)
        logger.info("Appended %d rows (%d total)", rows_added, len(merged_df))

        s3.upload_file(consolidated_path, s3_bucket, s3_key)
        logger.info("Uploaded consolidated CSV to s3://%s/%s", s3_bucket, s3_key)
        return {"status": "success", "action": "appended", "rows_added": rows_added}

    except Exception as e:
        logger.error("S3 CSV sync failed: %s", e)
        return {"status": "failed", "error": str(e)}
    finally:
        import os

        if consolidated_path and os.path.exists(consolidated_path):
            os.unlink(consolidated_path)


def upload_profiler_traces_to_s3(
    traces_dir: Path,
    *,
    model_name: str,
    accelerator: str,
    tp_size: int,
    version: str,
    profile_labels: list[str],
    s3_bucket: str,
    s3_prefix: str,
    vault_name: str,
    credentials_file: str = "aws.credentials",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload profiler traces to S3.

    Path: s3://{bucket}/{prefix}/{accel}/{model}/tp{N}/{version}/{profile_label}/
    """
    if not traces_dir.exists():
        return {"status": "skipped", "reason": f"traces dir not found: {traces_dir}"}

    trace_files = [
        f
        for f in traces_dir.iterdir()
        if f.is_file() and _is_uploadable_trace(f)
    ]
    if not trace_files:
        return {"status": "skipped", "reason": "no trace files found"}

    if dry_run:
        logger.info("DRY RUN: Would upload %d traces to S3", len(trace_files))
        return {"status": "success", "dry_run": True, "trace_count": len(trace_files)}

    from projects.caliper.cli.s3_export import create_s3_client, get_aws_credentials

    credentials_path = get_aws_credentials(vault_name, credentials_file)
    if not credentials_path:
        return {"status": "failed", "error": f"AWS credentials not found in vault {vault_name}"}

    accel_folder = _accelerator_s3_folder(accelerator)
    model_encoded = model_name.replace("/", "--")
    uploaded = 0

    try:
        s3 = create_s3_client(credentials_path)

        for trace_file in trace_files:
            matched_label = None
            for label in profile_labels:
                if label in trace_file.name:
                    matched_label = label
                    break
            if not matched_label:
                matched_label = profile_labels[0] if len(profile_labels) == 1 else None
            if not matched_label:
                logger.warning("Could not match trace %s to a profile label", trace_file.name)
                continue

            s3_key = (
                f"{s3_prefix}/{accel_folder}/{model_encoded}"
                f"/tp{tp_size}/{version}/{matched_label}/{trace_file.name}"
            )

            try:
                s3.head_object(Bucket=s3_bucket, Key=s3_key)
                s3.copy_object(
                    Bucket=s3_bucket,
                    CopySource={"Bucket": s3_bucket, "Key": s3_key},
                    Key=s3_key + ".bak",
                )
                logger.info("Backed up existing trace at s3://%s/%s", s3_bucket, s3_key)
            except Exception:
                pass

            s3.upload_file(str(trace_file), s3_bucket, s3_key)
            logger.info("Uploaded trace to s3://%s/%s", s3_bucket, s3_key)
            uploaded += 1

        return {"status": "success", "uploaded": uploaded}
    except Exception as e:
        logger.error("Trace upload failed: %s", e)
        return {"status": "failed", "error": str(e), "uploaded": uploaded}


def upload_predictor_log_to_s3(
    log_path: Path,
    *,
    run_uuid: str,
    s3_bucket: str,
    vault_name: str,
    credentials_file: str = "aws.credentials",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload the vLLM predictor pod log to S3 as ``logs/{run_uuid}.log``."""
    if not log_path.exists():
        return {"status": "skipped", "reason": f"log not found: {log_path}"}

    s3_key = f"logs/{run_uuid}.log"

    if dry_run:
        logger.info("DRY RUN: Would upload %s to s3://%s/%s", log_path, s3_bucket, s3_key)
        return {"status": "success", "dry_run": True}

    from projects.caliper.cli.s3_export import create_s3_client, get_aws_credentials

    credentials_path = get_aws_credentials(vault_name, credentials_file)
    if not credentials_path:
        return {"status": "failed", "error": f"AWS credentials not found in vault {vault_name}"}

    try:
        s3 = create_s3_client(credentials_path)
        s3.upload_file(str(log_path), s3_bucket, s3_key)
        logger.info("Uploaded predictor log to s3://%s/%s", s3_bucket, s3_key)
        return {"status": "success", "s3_key": s3_key}
    except Exception as e:
        logger.error("Predictor log upload failed: %s", e)
        return {"status": "failed", "error": str(e)}


def _is_uploadable_trace(path: Path) -> bool:
    """Rank-0 Chrome/Perfetto (and Proton/Nsight) artifacts after copy_profiler_traces."""
    name = path.name
    if not name.startswith("trace") or "rank0" not in name:
        return False
    return (
        name.endswith(".json")
        or name.endswith(".json.gz")
        or name.endswith(".gz")
        or name.endswith(".hatchet")
        or name.endswith(".nsys-rep")
        or name.endswith(".qdrep")
    )


def _accelerator_s3_folder(accelerator: str) -> str:
    upper = accelerator.upper()
    if upper.startswith("H200"):
        return "H200"
    if upper.startswith("B200"):
        return "B200"
    if upper.startswith("MI300"):
        return "MI300x"
    return accelerator
