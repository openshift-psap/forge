"""
Shared helpers for per-project Slack notification providers.

These utilities are framework-wide and can be used by any project's
notification provider to resolve test artifacts, read timing data,
extract labels, find KPIs, and build comparison tables.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from projects.core.notifications.provider import NotificationContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Artifact root resolution
# ---------------------------------------------------------------------------


def get_test_artifacts_root(context: NotificationContext) -> Path | None:
    """Resolve the root directory containing actual test results.

    In Fournos pipelines, each step (prepare, test, export) runs as a
    separate container with its own ARTIFACT_DIR. The export step's
    BASE_ARTIFACT_DIR is its own subdir, NOT the overall root.

    The correct root is ``caliper.export.from`` which is set by the
    export entrypoint to the shared workspace containing all steps.
    """
    try:
        from projects.core.library import config

        export_from = config.project.get_config(
            "caliper.export.from", None, print=False, warn=False
        )
        if export_from:
            p = Path(export_from)
            if p.exists():
                return p
    except Exception:
        pass

    base_env = os.environ.get("FORGE_BASE_ARTIFACT_DIR")
    if base_env:
        p = Path(base_env)
        if p.exists():
            return p

    if context.artifact_dir:
        parent = context.artifact_dir.parent
        if parent.exists() and any(parent.glob("*/__test_labels__.yaml")):
            return parent

    return context.artifact_dir


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def read_test_duration(context: NotificationContext) -> str:
    """Read formatted duration from 000__ci_metadata/test_duration.yaml."""
    root = get_test_artifacts_root(context)
    if not root:
        return ""

    timing_file = root / "000__ci_metadata" / "test_duration.yaml"
    if not timing_file.exists():
        candidates = list(root.glob("**/000__ci_metadata/test_duration.yaml"))
        if not candidates:
            return ""
        timing_file = candidates[0]

    try:
        with open(timing_file) as f:
            data = yaml.safe_load(f) or {}
        return data.get("duration", {}).get("formatted", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Test labels
# ---------------------------------------------------------------------------


def get_label_value(artifact_root: Path | None, key: str) -> str | None:
    """Extract a value from __test_labels__.yaml in the artifact tree.

    The labels file format is: {"version": "1", "labels": {"key": "value", ...}}
    """
    if not artifact_root:
        return None

    labels_file = artifact_root / "__test_labels__.yaml"
    if not labels_file.exists():
        labels_glob = list(artifact_root.glob("**/__test_labels__.yaml"))
        if not labels_glob:
            return None
        labels_file = labels_glob[0]

    try:
        with open(labels_file) as f:
            data = yaml.safe_load(f) or {}
        labels = data.get("labels", data)
        val = labels.get(key, "")
        return str(val) if val else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# KPI file discovery
# ---------------------------------------------------------------------------


def find_kpis_jsonl(artifact_dir: Path) -> Path | None:
    """Find kpis.jsonl in artifact directory tree."""
    direct = artifact_dir / "kpis.jsonl"
    if direct.exists():
        return direct

    for f in artifact_dir.glob("**/kpis.jsonl"):
        return f
    return None


# ---------------------------------------------------------------------------
# MLflow URL extraction
# ---------------------------------------------------------------------------


def extract_mlflow_url(context: NotificationContext) -> str | None:
    """Extract MLflow run URL from caliper export status dict."""
    if not isinstance(context.status, dict):
        return None
    backends = context.status.get("caliper_artifacts_export", {}).get("backends", {})
    mlflow_info = backends.get("mlflow", {})
    return mlflow_info.get("run_url") or mlflow_info.get("experiment_url")


# ---------------------------------------------------------------------------
# KPI comparison table formatting
# ---------------------------------------------------------------------------


def build_comparison_table(
    current: dict[str, float],
    previous: dict[str, float],
    prev_run_name: str,
    kpi_definitions: list[tuple[str, str, str]],
) -> str:
    """Format a Slack-friendly KPI comparison table.

    Args:
        current: Current run's KPI values keyed by kpi_id.
        previous: Previous run's KPI values keyed by kpi_id.
        prev_run_name: Display name of the previous run.
        kpi_definitions: List of (kpi_id, display_name, unit) tuples.
    """
    lines = [f"*KPI comparison* (vs `{prev_run_name}`):", "```"]

    header = f"{'KPI':<16}| {'Previous':>10} | {'Current':>10} | {'Delta':>8}"
    lines.append(header)
    lines.append("-" * len(header))

    for kpi_id, display_name, unit in kpi_definitions:
        cur_val = current.get(kpi_id)
        prev_val = previous.get(kpi_id)

        if cur_val is None:
            continue

        cur_str = format_kpi_value(cur_val, unit)
        prev_str = format_kpi_value(prev_val, unit) if prev_val is not None else "n/a"
        delta_str = format_kpi_delta(cur_val, prev_val) if prev_val is not None else "n/a"

        lines.append(f"{display_name:<16}| {prev_str:>10} | {cur_str:>10} | {delta_str:>8}")

    lines.append("```")
    return "\n".join(lines)


def build_current_kpis_list(
    current: dict[str, float],
    kpi_definitions: list[tuple[str, str, str]],
) -> str:
    """Format current KPIs as a simple bullet list (no comparison available)."""
    lines = ["*Current KPIs*:"]
    for kpi_id, display_name, unit in kpi_definitions:
        val = current.get(kpi_id)
        if val is None:
            continue
        lines.append(f"  \u2022 {display_name}: `{format_kpi_value(val, unit)}`")

    return "\n".join(lines) if len(lines) > 1 else ""


def format_kpi_value(value: float | None, unit: str) -> str:
    """Format a single KPI value with its unit."""
    if value is None:
        return "n/a"
    if unit == "%":
        return f"{value * 100:.2f}%"
    if unit == "ms":
        return f"{value:.1f} ms"
    if unit == "req/s":
        return f"{value:.0f}"
    return f"{value:.2f}"


def format_kpi_delta(current: float, previous: float) -> str:
    """Format the percentage delta between two KPI values."""
    if previous == 0:
        return "n/a"
    pct = ((current - previous) / abs(previous)) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"
