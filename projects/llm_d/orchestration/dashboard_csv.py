"""Append the llm-d dashboard CSV rows to the consolidated staging dashboard CSV on S3.

This is llm-d only for now. rhaiis does the same with its own code
(``projects/rhaiis/postprocess/s3_dashboard.py``); both should move to a shared
Caliper step.
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

from projects.core.library import config, env

logger = logging.getLogger(__name__)

CONFIG_KEY = "caliper.postprocess.csv_dashboard"

# The staging dashboard CSV predates the multi-turn columns, so they are dropped
# from the upload to keep the rows aligned with its header. Add them back once the
# staging CSV (and the dashboard reading it) has them.
EXCLUDED_COLUMNS = ("turn_index", "turns")

# Rows are not uploaded twice for the same run (re-run or replot of a test).
RUN_ID_COLUMN = "uuid"

MAX_WRITE_ATTEMPTS = 5
# S3 answers a conditional write that lost the race with one of these.
WRITE_CONFLICT_CODES = ("PreconditionFailed", "ConditionalRequestConflict")


class DashboardCsvError(RuntimeError):
    """The dashboard CSV rows could not be added to the consolidated CSV."""


def is_enabled() -> bool:
    return bool(config.project.get_config(f"{CONFIG_KEY}.enabled", False))


def list_vaults() -> list[str]:
    """Vaults needed to sync the dashboard CSV (for the Fournos vault resolution)."""
    if not is_enabled():
        return []
    return [config.project.get_config(f"{CONFIG_KEY}.vault.name")]


def find_dashboard_csv() -> Path:
    """Return the dashboard CSV of the latest Caliper postprocessing run."""
    postprocess_dirs = sorted(Path(env.ARTIFACT_DIR).glob("*__postprocessing"))
    if not postprocess_dirs:
        raise DashboardCsvError(f"No postprocessing directory found in {env.ARTIFACT_DIR}")

    csv_output = config.project.get_config("caliper.postprocess.kpi.dashboard_csv.output")
    csv_path = postprocess_dirs[-1] / csv_output
    if not csv_path.exists():
        raise DashboardCsvError(f"Dashboard CSV not found: {csv_path}")

    return csv_path


def merge_rows(existing_csv: str | None, new_csv: str) -> tuple[str, int]:
    """Append the rows of ``new_csv`` to ``existing_csv``.

    ``existing_csv`` is ``None`` when the consolidated CSV does not exist yet.
    Returns the merged CSV and the number of rows added. The existing content is
    kept as-is; new rows of a run already present (same ``uuid``) are skipped.
    """
    reader = csv.DictReader(io.StringIO(new_csv))
    columns = [name for name in reader.fieldnames or [] if name not in EXCLUDED_COLUMNS]
    if RUN_ID_COLUMN not in columns:
        raise DashboardCsvError(f"Dashboard CSV has no '{RUN_ID_COLUMN}' column")
    new_rows = list(reader)
    if any(not row[RUN_ID_COLUMN] for row in new_rows):
        raise DashboardCsvError(f"Dashboard CSV has rows without a '{RUN_ID_COLUMN}'")

    if existing_csv is None:
        existing_csv = ""
        known_runs: set[str] = set()
        write_header = True
    else:
        existing = csv.DictReader(io.StringIO(existing_csv))
        existing_columns = existing.fieldnames or []
        if existing_columns != columns:
            raise DashboardCsvError(
                "Dashboard CSV columns do not match the consolidated CSV: "
                f"missing {sorted(set(existing_columns) - set(columns))}, "
                f"unexpected {sorted(set(columns) - set(existing_columns))}, "
                f"or a different order"
            )
        known_runs = {row[RUN_ID_COLUMN] for row in existing}
        write_header = False
        if existing_csv and not existing_csv.endswith("\n"):
            existing_csv += "\n"

    rows_to_add = [row for row in new_rows if row[RUN_ID_COLUMN] not in known_runs]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    if write_header:
        writer.writeheader()
    writer.writerows(rows_to_add)

    return existing_csv + output.getvalue(), len(rows_to_add)


def sync_dashboard_csv(csv_path: Path) -> int:
    """Append ``csv_path`` rows to the consolidated CSV on S3. Returns the rows added."""
    from projects.caliper.cli.s3_export import create_s3_client, get_aws_credentials

    bucket = config.project.get_config(f"{CONFIG_KEY}.bucket")
    s3_key = config.project.get_config(f"{CONFIG_KEY}.s3_key")
    destination = f"s3://{bucket}/{s3_key}"

    if config.project.get_config("caliper.export.dry_run", False):
        logger.info("Dry run: would append %s to %s", csv_path, destination)
        return 0

    vault_name = config.project.get_config(f"{CONFIG_KEY}.vault.name")
    credentials_file = config.project.get_config(f"{CONFIG_KEY}.vault.credentials_file")
    credentials_path = get_aws_credentials(vault_name, credentials_file)
    if credentials_path is None:
        raise DashboardCsvError(f"AWS credentials not found in vault {vault_name}")

    s3 = create_s3_client(credentials_path)
    new_csv = csv_path.read_text(encoding="utf-8")

    # Another job can update the consolidated CSV between our read and write.
    # The write is conditional on the version we read (ETag), and the merge is
    # redone on the latest version when it changed in between.
    for _attempt in range(MAX_WRITE_ATTEMPTS):
        try:
            current = s3.get_object(Bucket=bucket, Key=s3_key)
            existing_csv = current["Body"].read().decode("utf-8")
            write_condition = {"IfMatch": current["ETag"]}
        except s3.exceptions.NoSuchKey:
            logger.warning("%s does not exist, creating it", destination)
            existing_csv = None
            write_condition = {"IfNoneMatch": "*"}

        merged_csv, rows_added = merge_rows(existing_csv, new_csv)
        if not rows_added:
            logger.info("No new rows to append to %s", destination)
            return 0

        try:
            s3.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=merged_csv.encode("utf-8"),
                ContentType="text/csv",
                **write_condition,
            )
        except s3.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") not in WRITE_CONFLICT_CODES:
                raise
            logger.info("%s changed while appending, retrying", destination)
            continue

        logger.info("Appended %d rows to %s", rows_added, destination)
        return rows_added

    raise DashboardCsvError(
        f"{destination} kept changing, could not append after {MAX_WRITE_ATTEMPTS} attempts"
    )
