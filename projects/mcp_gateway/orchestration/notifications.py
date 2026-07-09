"""
Per-project Slack notification provider for MCP Gateway.

Sends structured performance summaries with KPI comparison against the
previous MLflow run. Reuses shared helpers from the core notification
framework.

Channel ID is read from the project's config.yaml at
``notifications.slack.channel_id``.
"""

from __future__ import annotations

import json
import logging
import os
import re

from projects.core.library import config
from projects.core.notifications.helpers import (
    build_comparison_table,
    build_current_kpis_list,
    extract_mlflow_url,
    get_label_value,
    get_test_artifacts_root,
    read_test_duration,
)
from projects.core.notifications.provider import NotificationContext, SlackNotificationProvider
from projects.core.notifications.send import get_ocpci_link

logger = logging.getLogger(__name__)

TARGET_KPIS = [
    ("mcp_gw_requests_per_second", "RPS", "req/s"),
    ("mcp_gw_p95_ms", "P95 latency", "ms"),
    ("mcp_gw_p99_ms", "P99 latency", "ms"),
    ("mcp_gw_failure_rate", "Failure rate", "%"),
]

# Mapping from kpis.jsonl IDs to MLflow metrics.json keys (logged via mlflow.log_metric)
_KPI_TO_MLFLOW_METRIC = {
    "mcp_gw_requests_per_second": "requests_per_second",
    "mcp_gw_p95_ms": "p95_ms",
    "mcp_gw_p99_ms": "p99_ms",
    "mcp_gw_failure_rate": "failure_rate",
}

_RUN_NAME_PREFIX = "forge-mcp-gateway-"


def _parse_run_name(run_name: str) -> dict | None:
    """Parse a forge-mcp-gateway run name into components.

    Expected formats:
      forge-mcp-gateway-s150-u500-vsha-<hash>-YYYYMMDD-HHMMSS
      forge-mcp-gateway-s150-u500-v0.7.0-YYYYMMDD-HHMMSS
      forge-mcp-gateway-0.5.1-YYYYMMDD-HHMMSS

    Returns dict with keys: config, is_sha, version — or None if unparseable.
    """
    if not run_name.startswith(_RUN_NAME_PREFIX):
        return None

    rest = run_name[len(_RUN_NAME_PREFIX) :]

    config = None
    config_match = re.match(r"(s\d+-u\d+)-", rest)
    if config_match:
        config = config_match.group(1)
        rest = rest[config_match.end() :]

    is_sha = False
    version = None

    if rest.startswith("vsha-"):
        is_sha = True
        sha_match = re.match(r"vsha-([a-f0-9]+)-\d{8}-\d{6}$", rest)
        if sha_match:
            version = sha_match.group(1)
    else:
        version_match = re.match(r"v?(.+?)-\d{8}-\d{6}$", rest)
        if version_match:
            version = version_match.group(1)

    return {"config": config, "is_sha": is_sha, "version": version}


class MCPGatewaySlackProvider(SlackNotificationProvider):
    """Slack notification provider for the mcp_gateway project."""

    def get_channel_id(self) -> str:
        channel_id = config.project.get_config(
            "notifications.slack.channel_id", None, print=False, warn=False
        )
        if not channel_id:
            raise ValueError("notifications.slack.channel_id must be set in config.yaml")
        return channel_id

    def format_message(self, context: NotificationContext) -> str:
        header = _format_header(context)
        metadata = _format_metadata(context)
        kpi_table = _format_kpi_table(context)
        links = _format_standard_links(context)
        failure_info = _format_failure_info(context)

        parts = [header, metadata, kpi_table, links, failure_info]
        return "\n\n".join(filter(None, parts))

    def get_thread_anchor(self, context: NotificationContext) -> str:
        if context.pr_number:
            return f"Thread for mcp_gateway PR #{context.pr_number}"

        job_name = os.environ.get("FJOB_NAME") or os.environ.get("JOB_NAME_SAFE", "")
        if job_name:
            return f"Thread for mcp_gateway `{job_name}`"

        return "Thread for mcp_gateway run"


# ---------------------------------------------------------------------------
# Message sections (MCP Gateway specific)
# ---------------------------------------------------------------------------


def _format_header(context: NotificationContext) -> str:
    status_icon = ":done-circle-check:" if context.finish_reason == "success" else ":no-red-circle:"
    duration = read_test_duration(context)
    duration_str = f" after {duration}" if duration else ""
    return f"{status_icon} *mcp_gateway test finished{duration_str}* {status_icon}"


def _format_metadata(context: NotificationContext) -> str:
    version = os.environ.get("MCP_GATEWAY_VERSION", "")
    preset = os.environ.get("MCP_GATEWAY_PRESET", "")

    test_root = get_test_artifacts_root(context)
    if not version:
        version = get_label_value(test_root, "mcp_gateway_version") or "unknown"
    if not preset:
        preset = get_label_value(test_root, "preset") or "default"

    return f"*Version*: `{version}`  |  *Preset*: `{preset}`"


def _format_kpi_table(context: NotificationContext) -> str:
    """Build comparison table: current KPIs vs previous MLflow run."""
    current_kpis = _load_current_kpis(context)
    if not current_kpis:
        return ""

    # Extract current run_id from export status to avoid race with parallel jobs
    current_run_id = None
    if isinstance(context.status, dict):
        backends = context.status.get("caliper_artifacts_export", {}).get("backends", {})
        current_run_id = backends.get("mlflow", {}).get("run_id")

    previous_kpis, previous_run_name, skip = _load_previous_kpis_from_mlflow(current_run_id)

    if skip:
        context.extra["_skip_notification"] = True
        return ""

    if previous_kpis:
        return build_comparison_table(current_kpis, previous_kpis, previous_run_name, TARGET_KPIS)
    else:
        return build_current_kpis_list(current_kpis, TARGET_KPIS)


def _format_standard_links(context: NotificationContext) -> str:
    """Generate artifact links pointing to MLflow."""
    mlflow_url = extract_mlflow_url(context)
    if not mlflow_url:
        return ""

    return f"\u2022 <{mlflow_url}|MLflow run (results & logs)>"


def _format_failure_info(context: NotificationContext) -> str:
    """Include structured failure details when test failed."""
    if context.finish_reason == "success":
        return ""
    if not context.artifact_dir:
        return ""

    try:
        from projects.core.notifications.send import _get_notification_content

        def get_link(name, path, **kwargs):
            return f"<{get_ocpci_link(path, **kwargs)}|{name}>"

        def get_bold(text):
            return f"*{text}*"

        return _get_notification_content(context.artifact_dir, get_link, get_bold)
    except Exception as e:
        logger.warning("Failed to extract failure info: %s", e)
        return ""


# ---------------------------------------------------------------------------
# KPI loading (MCP Gateway specific)
# ---------------------------------------------------------------------------


def _find_kpis_json(artifact_dir):
    """Find kpis.json in artifact tree."""
    direct = artifact_dir / "kpis.json"
    if direct.exists():
        return direct
    for f in artifact_dir.glob("**/kpis.json"):
        return f

    return None


def _load_current_kpis(context: NotificationContext) -> dict[str, float]:
    """Read KPI values from kpis.json in the artifact directory."""
    test_root = get_test_artifacts_root(context)
    if not test_root:
        return {}

    kpis_file = _find_kpis_json(test_root)
    if not kpis_file:
        return {}

    target_ids = {k[0] for k in TARGET_KPIS}
    kpis: dict[str, float] = {}

    try:
        with open(kpis_file) as f:
            data = json.load(f)
            for test in data.get("tests", []):
                for kpi_record in test.get("kpis", []):
                    kpi_id = kpi_record.get("id", "")
                    if kpi_id in target_ids:
                        value = kpi_record.get("value")
                        if value is not None:
                            kpis[kpi_id] = float(value)
    except Exception as e:
        logger.warning("Failed to read KPI file %s: %s", kpis_file, e)

    return kpis


def _load_previous_kpis_from_mlflow(
    current_run_id: str | None = None,
) -> tuple[dict[str, float], str, bool]:
    """Query MLflow for the previous matching run's metrics.

    Args:
        current_run_id: MLflow run ID of this notification's run. When provided
            the current run is located by ID (avoids races with parallel jobs).

    Matching rules:
    - Same load config (e.g. s150-u500)
    - Same version type (SHA-based compared only to SHA-based, release to release)
    - Same preset parameter value

    Returns (metrics_dict, run_name, skip_notification):
    - skip_notification is True when the previous matching run has the same
      version/SHA (duplicate run, notification already sent).
    """
    try:
        from projects.caliper.engine.file_export.mlflow_secrets import (
            load_mlflow_secrets_yaml,
            mlflow_connection_env,
        )
        from projects.core.library import vault as vault_lib

        vault_name = config.project.get_config(
            "caliper.export.backend.mlflow.secrets.vault.name", None, print=False, warn=False
        )
        vault_secret = config.project.get_config(
            "caliper.export.backend.mlflow.secrets.vault.mlflow_secret",
            None,
            print=False,
            warn=False,
        )
        experiment_name = config.project.get_config(
            "caliper.export.backend.mlflow.config.experiment", None, print=False, warn=False
        )

        if not all([vault_name, vault_secret, experiment_name]):
            logger.info("MLflow config incomplete, skipping comparison")
            return {}, "", False

        secrets_path = vault_lib.get_vault_content_path(vault_name, vault_secret)
        if not secrets_path or not secrets_path.exists():
            logger.info("MLflow secrets not available, skipping comparison")
            return {}, "", False

        secrets = load_mlflow_secrets_yaml(secrets_path)

        with mlflow_connection_env(secrets):
            import mlflow

            client = mlflow.tracking.MlflowClient()
            exp = client.get_experiment_by_name(experiment_name)
            if not exp:
                logger.info("MLflow experiment '%s' not found", experiment_name)
                return {}, "", False

            runs = client.search_runs(
                experiment_ids=[exp.experiment_id],
                order_by=["start_time DESC"],
                max_results=50,
            )

            if len(runs) < 2:
                logger.info("No previous MLflow run found for comparison")
                return {}, "", False

            # Locate the current run explicitly by ID when available
            current_run = None
            if current_run_id:
                for run in runs:
                    if run.info.run_id == current_run_id:
                        current_run = run
                        break
                if current_run is None:
                    logger.info(
                        "Current run_id '%s' not found in recent runs, falling back to runs[0]",
                        current_run_id,
                    )
            if current_run is None:
                current_run = runs[0]

            current_name = getattr(current_run.info, "run_name", "") or current_run.info.run_id[:8]
            current_parsed = _parse_run_name(current_name)
            current_preset = (current_run.data.params or {}).get("preset", "")

            if not current_parsed:
                logger.info("Cannot parse current run name '%s', skipping comparison", current_name)
                return {}, "", False

            # Find previous run matching: same config, same type, same preset
            previous_run = None
            for run in runs:
                if run.info.run_id == current_run.info.run_id:
                    continue
                candidate_name = getattr(run.info, "run_name", "") or run.info.run_id[:8]
                candidate_parsed = _parse_run_name(candidate_name)
                if not candidate_parsed:
                    continue

                if candidate_parsed["config"] != current_parsed["config"]:
                    continue
                if candidate_parsed["is_sha"] != current_parsed["is_sha"]:
                    continue

                candidate_preset = (run.data.params or {}).get("preset", "")
                if candidate_preset != current_preset:
                    continue

                previous_run = run
                break

            if previous_run is None:
                logger.info(
                    "No previous run matches config=%s, is_sha=%s, preset=%s",
                    current_parsed["config"],
                    current_parsed["is_sha"],
                    current_preset,
                )
                return {}, "", False

            prev_name = getattr(previous_run.info, "run_name", "") or previous_run.info.run_id[:8]
            prev_parsed = _parse_run_name(prev_name)

            # Check for duplicate: same version/SHA means already notified
            if prev_parsed and prev_parsed["version"] == current_parsed["version"]:
                logger.info(
                    "Previous matching run has same version '%s', skipping notification",
                    current_parsed["version"],
                )
                return {}, "", True

            raw_metrics = previous_run.data.metrics or {}
            mlflow_to_kpi = {v: k for k, v in _KPI_TO_MLFLOW_METRIC.items()}
            metrics = {}
            for mlflow_key, value in raw_metrics.items():
                kpi_id = mlflow_to_kpi.get(mlflow_key)
                if kpi_id:
                    metrics[kpi_id] = value

            return metrics, prev_name, False

    except Exception as e:
        logger.warning("MLflow comparison unavailable: %s", e)
        return {}, "", False
