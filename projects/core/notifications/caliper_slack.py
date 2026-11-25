"""Default Slack notification provider driven by Caliper analyze output.

Projects only need ``notifications.slack.channel_id`` in config. Message body
is built from ``kpi_analyze.json`` plus status, metadata labels, and MLflow link.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from projects.core.notifications.helpers import (
    extract_mlflow_url,
    format_kpi_value,
    get_label_value,
    get_test_artifacts_root,
    read_test_duration,
)
from projects.core.notifications.provider import NotificationContext, SlackNotificationProvider

logger = logging.getLogger(__name__)

# Relative improvement beyond threshold is reported as improvement.
# Matches Caliper max_relative_regression semantics (fraction → percent).
_DEFAULT_INTERESTING_PCT = 10.0

_GROUP_LABEL_KEYS = (
    "num_servers",
    "users",
    "target",
    "preset",
    "protocol_mode",
    "profile",
    "model",
    "accelerator",
)


class CaliperSlackProvider(SlackNotificationProvider):
    """Config-driven Slack provider: channel_id + Caliper analyze report."""

    def get_channel_id(self) -> str:
        from projects.core.library import config

        channel_id = config.project.get_config(
            "notifications.slack.channel_id", None, print=False, warn=False
        )
        if not channel_id:
            raise ValueError("notifications.slack.channel_id must be set in config.yaml")
        if not isinstance(channel_id, str):
            raise ValueError(
                f"notifications.slack.channel_id must be a string, got {type(channel_id).__name__}"
            )
        return channel_id

    def should_notify(self, context: NotificationContext) -> bool:
        from projects.core.library import config

        if context.finish_reason != "success":
            return True
        if config.project.get_config(
            "notifications.slack.notify_always", False, print=False, warn=False
        ):
            return True
        report = load_analyze_report(context)
        if not report:
            return True
        return bool(interesting_results(report))

    def format_message(self, context: NotificationContext) -> str:
        return format_caliper_slack_message(context)


def format_caliper_slack_message(context: NotificationContext) -> str:
    """Build RHAIIS-style Slack body from export context + kpi_analyze.json."""
    report = load_analyze_report(context)
    interesting = interesting_results(report) if report else []

    if context.finish_reason != "success":
        icon = ":x:"
        headline = f"{context.project_name} test failed"
    elif interesting:
        has_reg = any(r.get("verdict") == "REGRESSION" for r in interesting)
        has_imp = any(_is_improvement(r) for r in interesting)
        if has_reg and has_imp:
            icon = ":warning:"
            headline = "Performance regressions and improvements detected"
        elif has_reg:
            icon = ":warning:"
            headline = "Performance regressions detected"
        else:
            icon = ":large_green_circle:"
            headline = "Performance improvements detected"
    else:
        icon = ":done-circle-check:"
        headline = f"{context.project_name} test finished"

    parts = [f"{icon} *{headline}*"]

    meta = _format_metadata(context, report)
    if meta:
        parts.append(meta)

    mlflow_url = extract_mlflow_url(context)
    if mlflow_url:
        parts.append(f"*MLflow:* <{mlflow_url}|View Run>")

    if interesting:
        parts.append("*Changes:*\n" + format_interesting_changes(interesting))
    elif context.finish_reason == "success" and report:
        verdict = (report.get("overall") or {}).get("verdict") or (
            report.get("analysis") or {}
        ).get("status", "")
        if verdict in ("NO_BASELINE",):
            parts.append("_No historical baseline for regression comparison._")
        elif verdict in ("PASS", "no_regression"):
            tested = (report.get("overall") or {}).get("total_tested", 0)
            parts.append(f"_No significant KPI changes ({tested} KPIs compared)._")

    return "\n".join(parts)


def load_analyze_report(context: NotificationContext) -> dict[str, Any] | None:
    """Load Caliper ``kpi_analyze.json`` from artifacts or config path.

    Returns None when no report file exists yet. Raises if a report file is
    present but unreadable or not a JSON object.
    """
    from projects.core.library import config

    root = get_test_artifacts_root(context)
    candidates: list[Path] = []

    configured = None
    if config.project is not None:
        configured = config.project.get_config(
            "caliper.postprocess.analyze.output", None, print=False, warn=False
        )

    if configured and root:
        candidates.append(root / configured)
    if root:
        candidates.append(root / "regression_analyze" / "kpi_analyze.json")
        candidates.append(root / "kpi_analyze.json")
        candidates.extend(sorted(root.glob("**/kpi_analyze.json")))
    if context.artifact_dir:
        candidates.append(context.artifact_dir / "regression_analyze" / "kpi_analyze.json")
        candidates.extend(sorted(context.artifact_dir.glob("**/kpi_analyze.json")))

    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise FileNotFoundError(f"Cannot resolve analyze report path {path}") from exc
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        return _read_analyze_json(resolved)

    return None


def _read_analyze_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Analyze report is not valid JSON: {path}") from exc
    except OSError as exc:
        raise FileNotFoundError(f"Failed to read analyze report: {path}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Analyze report must be a JSON object, got {type(data).__name__}: {path}")
    return data


def interesting_results(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return regression + improvement rows from a Caliper analyze report."""
    results = report.get("results") or []
    out: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        if row.get("verdict") == "REGRESSION" or _is_improvement(row):
            out.append(row)
    return out


def format_interesting_changes(rows: list[dict[str, Any]]) -> str:
    """Format grouped KPI changes for Slack."""
    by_group: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _group_key(row.get("labels") or {})
        by_group.setdefault(key, []).append(row)

    lines: list[str] = []
    for group in sorted(by_group):
        lines.append(f"\n*{group}:*")
        for row in by_group[group]:
            lines.append(_format_change_line(row))
    return "\n".join(lines).lstrip("\n")


def _unwrap_scalar(value: Any) -> float | None:
    """Unwrap a KPI value to a plain float.

    The analyze engine reports some fields (e.g. ``current_value``) as a
    nested ``{"value": x, "comparison_keys": {...}}`` object rather than a
    plain number. Accept either shape so a schema change here can't crash
    the whole job via an unhandled ``float(dict)`` TypeError.
    """
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _relative_change_pct(row: dict[str, Any]) -> float:
    """Relative change, as a percent, for *row*.

    The analyze engine reports the fraction under ``details.relative_change``
    (e.g. ``0.0349`` for +3.49%), not a top-level ``relative_change_pct``.
    """
    details = row.get("details") or {}
    fraction = _unwrap_scalar(details.get("relative_change"))
    return (fraction or 0.0) * 100.0


def _is_improvement(row: dict[str, Any]) -> bool:
    if row.get("verdict") == "REGRESSION":
        return False
    pct = _relative_change_pct(row)
    higher_is_better = bool(row.get("higher_is_better", True))
    threshold = _DEFAULT_INTERESTING_PCT
    if higher_is_better:
        return pct > threshold
    return pct < -threshold


def _group_key(labels: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in _GROUP_LABEL_KEYS:
        if key in labels and labels[key] not in (None, ""):
            parts.append(f"{key}={labels[key]}")
    return " / ".join(parts) if parts else "default"


def _format_change_line(row: dict[str, Any]) -> str:
    kpi_id = str(row.get("kpi_id") or "kpi")
    name = kpi_id.replace("mcp_gw_", "").replace("_", " ")
    pct = _relative_change_pct(row)
    details = row.get("details") or {}
    baseline = _unwrap_scalar(details.get("baseline_mean"))
    current = _unwrap_scalar(row.get("current_value"))
    unit = _guess_unit(kpi_id)

    baseline_s = format_kpi_value(baseline, unit) if baseline is not None else "n/a"
    current_s = format_kpi_value(current, unit) if current is not None else "n/a"

    if row.get("verdict") == "REGRESSION":
        direction = "dropped" if pct < 0 else "increased"
        return (
            f"  :red_circle: *{name}*: {direction} {abs(pct):.1f}% "
            f"({baseline_s} \u2192 {current_s})"
        )
    return (
        f"  :large_green_circle: *{name}*: improved {abs(pct):.1f}% "
        f"({baseline_s} \u2192 {current_s})"
    )


def _guess_unit(kpi_id: str) -> str:
    if kpi_id.endswith("_ms"):
        return "ms"
    if kpi_id.endswith("_rate") and "failure" in kpi_id:
        return "%"
    if kpi_id.endswith("_rps") or "per_second" in kpi_id:
        return "req/s"
    if kpi_id.endswith("_bytes"):
        return "bytes"
    if kpi_id.endswith("_cores"):
        return "cores"
    return ""


def _format_metadata(context: NotificationContext, report: dict[str, Any] | None) -> str:
    from projects.core.library import config

    lines: list[str] = []
    job_id = os.environ.get("FJOB_NAME") or os.environ.get("JOB_NAME_SAFE") or ""
    if job_id:
        lines.append(f"*Job:* `{job_id}`")

    root = get_test_artifacts_root(context)
    version = os.environ.get("MCP_GATEWAY_VERSION") or get_label_value(root, "mcp_gateway_version")
    preset = os.environ.get("MCP_GATEWAY_PRESET") or get_label_value(root, "preset")
    if not preset:
        preset = get_label_value(root, "selected_preset")

    compare_keys = []
    if report:
        compare_keys = (report.get("analysis") or {}).get("config", {}).get("comparison_keys") or []

    if version:
        baseline_hint = _baseline_version_hint(report, compare_keys)
        if baseline_hint:
            lines.append(f"*Versions:* `{version}` vs `{baseline_hint}` (baseline)")
        else:
            lines.append(f"*Version:* `{version}`")
    if preset:
        lines.append(f"*Preset:* `{preset}`")

    duration = read_test_duration(context)
    if duration:
        lines.append(f"*Duration:* {duration}")

    slack_user = ""
    if config.project is not None:
        slack_user = (
            config.project.get_config("notifications.slack.user", "", print=False, warn=False) or ""
        )
    if slack_user and re.match(r"^[UW][A-Z0-9]+$", slack_user):
        lines.insert(0, f"*Triggered by:* <@{slack_user}>")
    elif slack_user:
        lines.insert(0, f"*Triggered by:* {slack_user}")

    return "\n".join(lines)


def _baseline_version_hint(report: dict[str, Any] | None, compare_keys: list[str]) -> str | None:
    if not report or not compare_keys:
        return None
    results = report.get("results") or []
    if not results:
        return None
    baseline_values = results[0].get("baseline_values") or {}
    if not isinstance(baseline_values, dict) or not baseline_values:
        return None
    # Keys look like "mcp_gateway_version=0.6.2"
    first_flag = next(iter(baseline_values))
    if "=" in first_flag:
        return first_flag.split("=", 1)[1]
    return first_flag
