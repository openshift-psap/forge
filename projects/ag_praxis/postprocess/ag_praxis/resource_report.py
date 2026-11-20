from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from pathlib import Path

from projects.caliper.postprocess.helpers.visualization_utils import (
    compute_report_stem,
    create_report_filename,
    create_report_title_display,
    save_figure,
)
from projects.caliper.postprocess.prometheus.parser import CaptureGroupResult, MetricTimeSeries
from projects.caliper.postprocess.prometheus.plotting import create_combined_figure
from projects.caliper.postprocess.prometheus.report import assemble_report, render_variables_section

REPORT_TITLE = "Resource Usage vs Limits"

# (usage metric, limits metric, requests metric, section title)
METRIC_PAIRS = [
    (
        "cpu_usage_cores_by_pod",
        "cpu_limits_by_pod",
        "cpu_requests_by_pod",
        "CPU Usage vs Limits and Requests",
    ),
    (
        "memory_usage_bytes_by_pod",
        "memory_limits_by_pod",
        "memory_requests_by_pod",
        "Memory Usage vs Limits and Requests",
    ),
]

_GROUP_PREFIX_RE = re.compile(r"^\d+__capture_prometheus_metrics_")


@dataclass(frozen=True)
class ResourceMetricPair:
    usage: MetricTimeSeries
    limits: MetricTimeSeries
    requests: MetricTimeSeries | None
    title: str


def find_resource_pairs(group_result: CaptureGroupResult) -> list[ResourceMetricPair]:
    """Return the complete usage/limits pairs captured in a group.

    A pair requires both the usage and the limits metric; the requests
    metric is optional. Groups without any complete pair are not
    resource usage/limits captures (eg vllm latency, worker node).
    """
    metrics_by_name = {m.metric_name: m for m in group_result.metrics}

    pairs = []
    for usage_name, limits_name, requests_name, title in METRIC_PAIRS:
        usage = metrics_by_name.get(usage_name)
        limits = metrics_by_name.get(limits_name)
        if not usage or not limits:
            continue
        pairs.append(
            ResourceMetricPair(
                usage=usage,
                limits=limits,
                requests=metrics_by_name.get(requests_name),
                title=title,
            )
        )
    return pairs


def generate_resource_report(
    group_results: list[CaptureGroupResult],
    output_dir: str | Path,
    report_number: int | None = None,
) -> Path | None:
    """Generate a single resource usage/limits report page for a test node.

    Every capture group that contains at least one complete usage/limits
    pair gets a section in the report.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    qualifying = [
        (group, pairs) for group in group_results if (pairs := find_resource_pairs(group))
    ]
    if not qualifying:
        return None

    display_title = create_report_title_display(REPORT_TITLE)
    report_stem = compute_report_stem(REPORT_TITLE, report_number)

    report_dir = output_dir / report_stem
    report_dir.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []

    for group, pairs in qualifying:
        sections.append(_render_group_section(group, pairs, report_dir, report_stem))

    report_html = assemble_report(display_title, sections)
    report_path = output_dir / create_report_filename(REPORT_TITLE, report_number)
    report_path.write_text(report_html, encoding="utf-8")

    return report_path


def _render_group_section(
    group: CaptureGroupResult,
    pairs: list[ResourceMetricPair],
    report_dir: Path,
    report_stem: str,
) -> str:
    group_slug = _group_slug(group.group_name)
    group_asset_dir = report_dir / group_slug
    group_asset_dir.mkdir(parents=True, exist_ok=True)

    parts = [
        '<div class="metric-section">',
        f"<h2>{html_module.escape(_group_display_name(group.group_name))}</h2>",
    ]

    if group.variables:
        parts.append(render_variables_section(group.variables))

    for idx, pair in enumerate(pairs):
        parts.append(_render_pair_section(pair, group_asset_dir, report_stem, group_slug, idx))

    parts.append("</div>")
    return "\n".join(parts)


def _render_pair_section(
    pair: ResourceMetricPair,
    group_asset_dir: Path,
    report_stem: str,
    group_slug: str,
    idx: int,
) -> str:
    if not pair.usage.series and not pair.limits.series:
        return (
            f'<div class="metric-section">'
            f"<h3>{html_module.escape(pair.title)}</h3>"
            f'<p class="no-data">No data available.</p>'
            f"</div>"
        )

    fig = create_combined_figure(pair.usage, pair.limits, pair.title, pair.requests)
    filename = f"{idx:03d}__{pair.usage.metric_name}_vs_{pair.limits.metric_name}"

    save_figure(fig, group_asset_dir, filename, as_image=True)
    html_path = save_figure(fig, group_asset_dir, filename, as_image=False)

    inline_plot = fig.to_html(include_plotlyjs=False, full_html=False)

    link_html = ""
    if html_path:
        html_rel = f"{report_stem}/{group_slug}/{Path(html_path).name}"
        link_html = f'<p><a href="{html_rel}">Open interactive plot</a></p>'

    return (
        f'<div class="metric-subsection">'
        f"<h3>{html_module.escape(pair.title)}</h3>"
        f"{link_html}{inline_plot}"
        f"</div>"
    )


def _group_slug(group_name: str) -> str:
    """Filesystem-friendly name for a capture group, eg '005__capture_prometheus_metrics_resource_kuadrant' -> 'resource_kuadrant'."""
    stripped = _GROUP_PREFIX_RE.sub("", group_name)
    return stripped.replace("-", "_").replace("/", "_")


def _group_display_name(group_name: str) -> str:
    """Human-friendly name for a capture group, eg '005__capture_prometheus_metrics_resource_kuadrant' -> 'Kuadrant'."""
    stripped = _GROUP_PREFIX_RE.sub("", group_name).removeprefix("resource_")
    return stripped.replace("-", " ").replace("_", " ").title()
