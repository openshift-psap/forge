"""RHAIIS-specific regression analysis and Slack notifications.

Temporary home for CSV-based regression comparison ported from model-furnace.
Will be replaced by generic Caliper KPI-based analysis once the framework lands.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import projects.core.notifications.slack.api as slack_api
from projects.core.library import vault

logger = logging.getLogger(__name__)


RHAIIS_SLACK_CHANNEL_ID = "C0B9T6JUW74"
CHAI_BOT_USER_ID = "U0AKNPBBVT7"  # Chai Bot — automated failure triage

_SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]+$")
_SLACK_GROUP_RE = re.compile(r"^S[A-Z0-9]+$")


def _format_slack_user_line(slack_user: str) -> str:
    """Build the 'Triggered by' line, supporting both user and group IDs."""
    if not slack_user:
        return ""
    if _SLACK_USER_RE.match(slack_user):
        return f"*Triggered by:* <@{slack_user}>\n"
    if _SLACK_GROUP_RE.match(slack_user):
        return f"*Triggered by:* <!subteam^{slack_user}>\n"
    return f"*Triggered by:* {slack_user}\n"


def _format_owner_line(owner: str) -> str:
    """Build the owner line for RHAIIS Slack notifications."""
    return f"*Owner:* {owner}\n" if owner else ""


def _send_via_topsail_bot(
    message: str, *, notification_vault: str | None = None, channel_id: str | None = None
) -> bool:
    """Send a Slack message using the topsail bot token from the forge notifications vault."""
    vault_name = notification_vault or "psap-forge-notifications"
    try:
        token_path = vault.get_vault_content_path(vault_name, "topsail-bot.slack-token")
    except Exception:
        logger.warning("Cannot resolve topsail bot token from vault %s", vault_name)
        return False

    if not token_path or not token_path.exists():
        logger.warning("topsail-bot.slack-token not found in vault %s", vault_name)
        return False

    token = token_path.read_text().strip()
    client = slack_api.init_client(token)
    if not client:
        logger.error("Failed to init Slack client with topsail bot token")
        return False

    _, ok = slack_api.send_message(client, message=message, channel_id=channel_id)
    return ok


# ---------------------------------------------------------------------------
# CSV-based regression comparison (ported from model-furnace, temporary)
# ---------------------------------------------------------------------------


@dataclass
class RegressionResult:
    profile: str
    metric_column: str
    metric_label: str
    current_value: float
    baseline_value: float
    pct_diff: float
    is_regression: bool
    is_improvement: bool = False


def geometric_mean(values) -> float | None:
    import numpy as np

    positive = (
        values[values > 0].values if hasattr(values, "values") else [v for v in values if v > 0]
    )
    if len(positive) == 0:
        return None
    return float(np.exp(np.mean(np.log(positive))))


def compare_runs(
    current_csv_path: str,
    consolidated_csv_path: str,
    compare_version: str,
    current_version: str,
    profile_map: dict[tuple[int, int], str],
    metrics: dict[str, dict[str, Any]],
    restrict_profiles: list[str] | None = None,
) -> tuple[list[RegressionResult], str]:
    """Compare current run metrics against a baseline version.

    Args:
        profile_map: Mapping of (input_tokens, output_tokens) -> profile name.
        metrics: Mapping of CSV column -> {label, higher_is_better, threshold}.

    Returns (results, skip_reason). If skip_reason is non-empty, comparison was skipped.
    """
    import pandas as pd

    current_df = pd.read_csv(current_csv_path, on_bad_lines="warn")
    baseline_df = pd.read_csv(consolidated_csv_path, on_bad_lines="warn")

    for col in current_df.columns:
        if current_df[col].dtype == object:
            current_df[col] = current_df[col].str.strip()
    for col in baseline_df.columns:
        if baseline_df[col].dtype == object:
            baseline_df[col] = baseline_df[col].str.strip()

    if current_df.empty:
        return [], "Current run produced no benchmark data"

    model = current_df["model"].iloc[0]
    accelerator = current_df["accelerator"].iloc[0]
    tp = current_df["TP"].iloc[0]

    logger.info(
        "Comparing %s vs %s for model=%s, accelerator=%s, TP=%s",
        current_version,
        compare_version,
        model,
        accelerator,
        tp,
    )

    def _assign_profile(row):
        try:
            isl = int(float(row["prompt toks"]))
            osl = int(float(row["output toks"]))
        except (ValueError, TypeError):
            return None
        return profile_map.get((isl, osl))

    current_df["_profile"] = current_df.apply(_assign_profile, axis=1)
    baseline_df["_profile"] = baseline_df.apply(_assign_profile, axis=1)

    baseline_filtered = baseline_df[
        (baseline_df["version"] == compare_version)
        & (baseline_df["model"] == model)
        & (baseline_df["accelerator"] == accelerator)
        & (pd.to_numeric(baseline_df["TP"], errors="coerce") == pd.to_numeric(tp, errors="coerce"))
    ]

    if baseline_filtered.empty:
        return (
            [],
            f"No baseline data for {compare_version} with model={model}, accelerator={accelerator}, TP={tp}",
        )

    current_profiles = set(current_df["_profile"].dropna().unique())
    baseline_profiles = set(baseline_filtered["_profile"].dropna().unique())
    common_profiles = sorted(current_profiles & baseline_profiles)

    if restrict_profiles:
        common_profiles = [p for p in common_profiles if p in restrict_profiles]

    if not common_profiles:
        return [], "No common ISL/OSL profiles between current and baseline"

    results: list[RegressionResult] = []

    for profile in common_profiles:
        cur = current_df[current_df["_profile"] == profile].copy()
        base = baseline_filtered[baseline_filtered["_profile"] == profile].copy()

        cur["intended concurrency"] = pd.to_numeric(cur["intended concurrency"], errors="coerce")
        base["intended concurrency"] = pd.to_numeric(base["intended concurrency"], errors="coerce")

        common_conc = set(cur["intended concurrency"].dropna().unique()) & set(
            base["intended concurrency"].dropna().unique()
        )
        common_conc.discard(1)

        if not common_conc:
            continue

        cur_common = cur[cur["intended concurrency"].isin(common_conc)]
        base_common = base[base["intended concurrency"].isin(common_conc)]

        for col, meta in metrics.items():
            if col not in cur_common.columns or col not in base_common.columns:
                continue

            cur_vals = pd.to_numeric(cur_common[col], errors="coerce").dropna()
            base_vals = pd.to_numeric(base_common[col], errors="coerce").dropna()

            cur_gm = geometric_mean(cur_vals)
            base_gm = geometric_mean(base_vals)

            if cur_gm is None or base_gm is None:
                continue

            pct_diff = ((cur_gm - base_gm) / base_gm) * 100
            threshold = meta["threshold"]

            if meta["higher_is_better"]:
                is_regression = pct_diff < -threshold
                is_improvement = pct_diff > threshold
            else:
                is_regression = pct_diff > threshold
                is_improvement = pct_diff < -threshold

            results.append(
                RegressionResult(
                    profile=profile,
                    metric_column=col,
                    metric_label=meta["label"],
                    current_value=cur_gm,
                    baseline_value=base_gm,
                    pct_diff=pct_diff,
                    is_regression=is_regression,
                    is_improvement=is_improvement,
                )
            )

    regressions = [r for r in results if r.is_regression]
    logger.info(
        "Comparison complete: %d metric comparisons, %d regressions", len(results), len(regressions)
    )
    return results, ""


def run_regression_analysis(
    current_csv_path: Path,
    consolidated_csv_path: Path,
    compare_version: str,
    current_version: str,
    output_file: Path,
    profile_map: dict[tuple[int, int], str],
    metrics: dict[str, dict[str, Any]],
    restrict_profiles: list[str] | None = None,
) -> dict[str, Any]:
    """Run regression analysis comparing current run against baseline.

    Returns dict with status, regressions, improvements, and skip_reason.
    """
    try:
        results, skip_reason = compare_runs(
            str(current_csv_path),
            str(consolidated_csv_path),
            compare_version,
            current_version,
            profile_map=profile_map,
            metrics=metrics,
            restrict_profiles=restrict_profiles,
        )

        if skip_reason:
            analysis = {
                "status": "skipped",
                "reason": skip_reason,
                "current_version": current_version,
                "compare_version": compare_version,
            }
        else:
            regressions = [r for r in results if r.is_regression]
            improvements = [r for r in results if r.is_improvement]

            analysis = {
                "status": "completed",
                "current_version": current_version,
                "compare_version": compare_version,
                "total_comparisons": len(results),
                "regression_count": len(regressions),
                "improvement_count": len(improvements),
                "regressions": [
                    {
                        "profile": r.profile,
                        "metric": r.metric_label,
                        "current": round(r.current_value, 2),
                        "baseline": round(r.baseline_value, 2),
                        "pct_diff": round(r.pct_diff, 1),
                    }
                    for r in regressions
                ],
                "improvements": [
                    {
                        "profile": r.profile,
                        "metric": r.metric_label,
                        "current": round(r.current_value, 2),
                        "baseline": round(r.baseline_value, 2),
                        "pct_diff": round(r.pct_diff, 1),
                    }
                    for r in improvements
                ],
                "all_results": [
                    {
                        "profile": r.profile,
                        "metric": r.metric_label,
                        "metric_column": r.metric_column,
                        "current": round(r.current_value, 2),
                        "baseline": round(r.baseline_value, 2),
                        "pct_diff": round(r.pct_diff, 1),
                        "is_regression": r.is_regression,
                        "is_improvement": r.is_improvement,
                    }
                    for r in results
                ],
            }

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            json.dump(analysis, f, indent=2)

        return analysis

    except Exception as e:
        logger.exception("Regression analysis failed")
        return {"status": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# RHAIIS constants
# ---------------------------------------------------------------------------

PROFILE_MAP = {
    (1000, 1000): "profile1",
    (512, 2048): "profile2",
    (2048, 128): "profile3",
    (8000, 1000): "profile4",
}

METRICS = {
    "total_tok/sec": {"label": "Total Throughput", "higher_is_better": True, "threshold": 5},
    "output_tok/sec": {"label": "Output Throughput", "higher_is_better": True, "threshold": 5},
    "ttft_p95": {"label": "TTFT P95", "higher_is_better": False, "threshold": 10},
    "itl_p95": {"label": "ITL P95", "higher_is_better": False, "threshold": 5},
    "request_latency_median": {
        "label": "Median E2E Latency",
        "higher_is_better": False,
        "threshold": 5,
    },
}

DASHBOARD_BASE_URL = "https://staging-aidash.apps.ocp4.intlab.redhat.com/"


def _build_mlflow_run_url() -> str:
    """Construct the MLflow run URL at runtime from vault secrets and config."""
    from projects.caliper.orchestration.export import build_mlflow_run_url_from_config

    try:
        return build_mlflow_run_url_from_config() or ""
    except Exception:
        logger.warning("Failed to build MLflow run URL from test labels", exc_info=True)
        return ""


PROFILE_DISPLAY_NAMES = {
    "profile1": "Profile A: Balanced (1k/1k)",
    "profile2": "Profile B: Variable Workload (512/2k)",
    "profile3": "Profile C: Large Prompt (2k/128)",
    "profile4": "Profile D: Prefill Heavy (8k/1k)",
}


def _build_dashboard_url(
    *,
    model: str = "",
    accelerator: str = "",
    current_version: str = "",
    compare_version: str = "",
    profiles: list[str] | None = None,
    tp: str = "",
) -> str:
    """Build a RHAIIS dashboard URL with filters pre-selected."""
    from urllib.parse import quote, urlencode

    params: dict[str, str] = {"view": "RHAIIS Dashboard"}

    if accelerator:
        params["accelerators"] = accelerator
    if model:
        params["models"] = model

    versions = ",".join(v for v in [current_version, compare_version] if v)
    if versions:
        params["versions"] = versions

    if profiles:
        display_names = [PROFILE_DISPLAY_NAMES.get(p, p) for p in profiles]
        params["profile"] = ",".join(display_names)

    if tp:
        try:
            params["tp_sizes"] = f"{float(tp):.1f}"
        except (ValueError, TypeError):
            params["tp_sizes"] = tp

    params["section"] = "performance_plots"

    return f"{DASHBOARD_BASE_URL}?{urlencode(params, quote_via=quote)}"


def send_regression_notification(
    analysis_result: dict,
    *,
    model: str = "",
    accelerator: str = "",
    job_id: str = "",
    slack_user: str = "",
    owner: str = "",
    notification_vault: str | None = None,
    dry_run: bool = False,
    report_url: str = "",
    tp: str = "",
    dp: str = "",
) -> bool:
    """Send regression/improvement Slack notification from analysis results.

    Args:
        analysis_result: Output from run_regression_analysis()
        model: Model name for display
        accelerator: Accelerator name for display
        job_id: Job identifier
        slack_user: Slack user ID (e.g. U01ABC123) to @-mention, or display name
        owner: Fournos job owner
        notification_vault: Vault containing topsail-bot.slack-token
        dry_run: Log only, don't send
        report_url: Optional presigned URL to an agent analysis report
        tp: Tensor parallelism size for display
        dp: Data parallelism size for display

    Returns:
        True if notification sent successfully
    """
    status = analysis_result.get("status")
    if status == "skipped":
        reason = analysis_result.get("reason", "unknown")
        logger.info("Regression analysis skipped: %s", reason)
        return True

    if status != "completed":
        return True

    regressions = analysis_result.get("regressions", [])
    improvements = analysis_result.get("improvements", [])

    if not regressions and not improvements:
        logger.info("No regressions or improvements to report")
        return True

    current_version = analysis_result.get("current_version", "")
    compare_version = analysis_result.get("compare_version", "")

    if regressions and improvements:
        icon = ":warning:"
        headline = "Performance regressions and improvements detected"
    elif regressions:
        icon = ":warning:"
        headline = "Performance regressions detected"
    else:
        icon = ":large_green_circle:"
        headline = "Performance improvements detected"

    detail_lines = []
    all_results = analysis_result.get("all_results", [])
    profiles = sorted(
        {r["profile"] for r in all_results if r.get("is_regression") or r.get("is_improvement")}
    )

    for profile in profiles:
        profile_results = [
            r
            for r in all_results
            if r["profile"] == profile and (r.get("is_regression") or r.get("is_improvement"))
        ]
        detail_lines.append(f"\n*{profile}:*")
        for r in profile_results:
            if r.get("is_regression"):
                direction = "dropped" if r["pct_diff"] < 0 else "increased"
                detail_lines.append(
                    f"  :red_circle: *{r['metric']}*: {direction} {abs(r['pct_diff']):.1f}% "
                    f"({r['baseline']:.2f} \u2192 {r['current']:.2f})"
                )
            else:
                detail_lines.append(
                    f"  :large_green_circle: *{r['metric']}*: improved {abs(r['pct_diff']):.1f}% "
                    f"({r['baseline']:.2f} \u2192 {r['current']:.2f})"
                )

    details = "\n".join(detail_lines)

    user_line = _format_slack_user_line(slack_user)
    owner_line = _format_owner_line(owner)

    report_line = f"*Agent Analysis:* <{report_url}|View Report>\n" if report_url else ""

    parallelism_line = ""
    parallelism_parts = []
    if tp:
        parallelism_parts.append(f"TP={tp}")
    if dp:
        parallelism_parts.append(f"DP={dp}")
    if parallelism_parts:
        parallelism_line = f"*Parallelism:* {', '.join(parallelism_parts)}\n"

    dashboard_url = _build_dashboard_url(
        model=model,
        accelerator=accelerator,
        current_version=current_version,
        compare_version=compare_version,
        profiles=profiles if profiles else None,
        tp=tp,
    )
    dashboard_line = f"*Dashboard:* <{dashboard_url}|View Dashboard>\n"

    mlflow_url = _build_mlflow_run_url()
    mlflow_line = f"*MLflow:* <{mlflow_url}|View Run>\n" if mlflow_url else ""

    message = (
        f"{icon} *{headline}*\n"
        f"{user_line}"
        f"{owner_line}"
        f"*Job:* `{job_id}`\n"
        f"*Model:* {model}\n"
        f"*Accelerator:* {accelerator}\n"
        f"{parallelism_line}"
        f"*Versions:* {current_version} vs {compare_version} (baseline)\n"
        f"{report_line}"
        f"{dashboard_line}"
        f"{mlflow_line}"
        f"*Changes:*\n{details}"
        f"\n<@{CHAI_BOT_USER_ID}>"
    )

    if dry_run:
        logger.info("DRY RUN regression notification:\n%s", message)
        return True

    return _send_via_topsail_bot(
        message, notification_vault=notification_vault, channel_id=RHAIIS_SLACK_CHANNEL_ID
    )


def send_success_notification(
    *,
    model: str = "",
    accelerator: str = "",
    job_id: str = "",
    slack_user: str = "",
    owner: str = "",
    notification_vault: str | None = None,
    dry_run: bool = False,
    tp: str = "",
    dp: str = "",
    version: str = "",
    workload_keys: list[str] | None = None,
    cluster: str = "",
    engine: str = "",
) -> bool:
    """Send a Slack notification when the RHAIIS pipeline succeeds with no regressions.

    Args:
        owner: Fournos job owner

    Returns:
        True if notification sent successfully
    """
    user_line = _format_slack_user_line(slack_user)
    owner_line = _format_owner_line(owner)

    parallelism_parts = []
    if tp:
        parallelism_parts.append(f"TP={tp}")
    if dp:
        parallelism_parts.append(f"DP={dp}")
    parallelism_line = (
        f"*Parallelism:* {', '.join(parallelism_parts)}\n" if parallelism_parts else ""
    )

    profiles_line = ""
    if workload_keys:
        profiles_line = f"*Workloads:* {', '.join(workload_keys)}\n"

    cluster_line = f"*Cluster:* {cluster}\n" if cluster else ""
    version_line = f"*Version:* {version}\n" if version else ""
    engine_line = f"*Engine:* {engine}\n" if engine else ""

    dashboard_line = ""
    try:
        from projects.core.library import config

        if config.project.get_config("caliper.postprocess.csv_dashboard.enabled", False):
            dashboard_url = _build_dashboard_url(
                model=model,
                accelerator=accelerator,
                current_version=version,
                profiles=workload_keys,
                tp=tp,
            )
            dashboard_line = f"*Dashboard:* <{dashboard_url}|View Dashboard>\n"
    except Exception:
        logger.warning("Failed to build dashboard URL for notification", exc_info=True)

    mlflow_url = _build_mlflow_run_url()
    mlflow_line = f"*MLflow:* <{mlflow_url}|View Run>\n" if mlflow_url else ""

    message = (
        f":white_check_mark: *RHAIIS Pipeline Succeeded*\n"
        f"{user_line}"
        f"{owner_line}"
        f"*Job:* `{job_id}`\n"
        f"*Model:* {model}\n"
        f"*Accelerator:* {accelerator}\n"
        f"{parallelism_line}"
        f"{engine_line}"
        f"{version_line}"
        f"{cluster_line}"
        f"{profiles_line}"
        f"{dashboard_line}"
        f"{mlflow_line}"
    )

    if dry_run:
        logger.info("DRY RUN success notification:\n%s", message)
        return True

    return _send_via_topsail_bot(
        message, notification_vault=notification_vault, channel_id=RHAIIS_SLACK_CHANNEL_ID
    )


def send_failure_notification(
    *,
    error: str,
    model: str = "",
    accelerator: str = "",
    job_id: str = "",
    slack_user: str = "",
    owner: str = "",
    notification_vault: str | None = None,
    dry_run: bool = False,
    tp: str = "",
    dp: str = "",
    version: str = "",
    workload_keys: list[str] | None = None,
    cluster: str = "",
    engine: str = "",
) -> bool:
    """Send a Slack alert when the RHAIIS pipeline fails.

    Args:
        error: Error message or traceback summary
        model: Model name for display
        accelerator: Accelerator name for display
        job_id: FournosJob name
        slack_user: Slack user ID to @-mention
        owner: Fournos job owner
        notification_vault: Vault containing topsail-bot.slack-token
        dry_run: Log only, don't send
        tp: Tensor parallelism size
        dp: Data parallelism size
        version: vLLM / RHAIIS version string
        workload_keys: List of workload profile keys
        cluster: Cluster name the job ran on

    Returns:
        True if notification sent successfully
    """
    user_line = _format_slack_user_line(slack_user)
    owner_line = _format_owner_line(owner)

    parallelism_parts = []
    if tp:
        parallelism_parts.append(f"TP={tp}")
    if dp:
        parallelism_parts.append(f"DP={dp}")
    parallelism_line = (
        f"*Parallelism:* {', '.join(parallelism_parts)}\n" if parallelism_parts else ""
    )

    profiles_line = ""
    if workload_keys:
        profiles_line = f"*Workloads:* {', '.join(workload_keys)}\n"

    cluster_line = f"*Cluster:* {cluster}\n" if cluster else ""
    version_line = f"*Version:* {version}\n" if version else ""
    engine_line = f"*Engine:* {engine}\n" if engine else ""

    error_text = error if len(error) <= 500 else error[:500] + "..."

    mlflow_url = _build_mlflow_run_url()
    mlflow_line = f"*MLflow:* <{mlflow_url}|View Run>\n" if mlflow_url else ""

    message = (
        f":x: *RHAIIS Pipeline Failed*\n"
        f"{user_line}"
        f"{owner_line}"
        f"*Job:* `{job_id}`\n"
        f"*Model:* {model}\n"
        f"*Accelerator:* {accelerator}\n"
        f"{parallelism_line}"
        f"{engine_line}"
        f"{version_line}"
        f"{cluster_line}"
        f"{profiles_line}"
        f"{mlflow_line}"
        f"*Error:*\n```{error_text}```"
        f"\n<@{CHAI_BOT_USER_ID}>"
    )

    if dry_run:
        logger.info("DRY RUN failure notification:\n%s", message)
        return True

    return _send_via_topsail_bot(
        message, notification_vault=notification_vault, channel_id=RHAIIS_SLACK_CHANNEL_ID
    )
