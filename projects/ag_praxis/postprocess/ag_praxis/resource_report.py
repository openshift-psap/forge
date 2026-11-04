from __future__ import annotations

import html as html_module
from pathlib import Path

from projects.caliper.postprocess.helpers.visualization_utils import (
    create_report_filename,
    create_report_title_display,
    save_figure,
)
from projects.caliper.postprocess.prometheus.parser import CaptureGroupResult
from projects.caliper.postprocess.prometheus.plotting import create_combined_figure
from projects.caliper.postprocess.prometheus.report import assemble_report, render_variables_section

METRIC_PAIRS = [
    ("cpu_usage_cores_by_pod", "cpu_limits_by_pod", "CPU Usage vs Limits"),
    ("memory_usage_bytes_by_pod", "memory_limits_by_pod", "Memory Usage vs Limits"),
]


def generate_resource_report(
    group_result: CaptureGroupResult,
    output_dir: str | Path,
    report_number: int | None = None,
) -> Path | None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    metrics_by_name = {m.metric_name: m for m in group_result.metrics}

    report_title = f"Resource Usage vs Limits: {group_result.group_name}"
    display_title = create_report_title_display(report_title, report_number)

    report_dir_name = report_title.lower().replace(" ", "_").replace(":", "").replace("-", "_")
    if report_number is not None:
        report_dir_name = f"report_{report_number:02d}_{report_dir_name}"
    report_dir = output_dir / report_dir_name
    report_dir.mkdir(parents=True, exist_ok=True)

    plot_sections: list[str] = []

    if group_result.variables:
        plot_sections.append(render_variables_section(group_result.variables))

    for idx, (usage_name, limit_name, title) in enumerate(METRIC_PAIRS):
        usage_metric = metrics_by_name.get(usage_name)
        limit_metric = metrics_by_name.get(limit_name)

        if not usage_metric or not limit_metric:
            continue

        if not usage_metric.series and not limit_metric.series:
            plot_sections.append(
                f'<div class="metric-section">'
                f"<h2>{html_module.escape(title)}</h2>"
                f'<p class="no-data">No data available.</p>'
                f"</div>"
            )
            continue

        fig = create_combined_figure(usage_metric, limit_metric, title)
        filename = f"{idx:03d}__{usage_name}_vs_{limit_name}"

        save_figure(fig, report_dir, filename, as_image=True)
        html_path = save_figure(fig, report_dir, filename, as_image=False)

        inline_plot = fig.to_html(include_plotlyjs=False, full_html=False)

        link_html = ""
        if html_path:
            html_rel = f"{report_dir_name}/{Path(html_path).name}"
            link_html = f'<p><a href="{html_rel}">Open interactive plot</a></p>'

        plot_sections.append(
            f'<div class="metric-section">'
            f"<h2>{html_module.escape(title)}</h2>"
            f"{link_html}{inline_plot}"
            f"</div>"
        )

    if not plot_sections:
        return None

    report_html = assemble_report(display_title, plot_sections)

    report_filename = create_report_filename(report_dir_name, report_number, report_title, "html")
    report_path = output_dir / report_filename
    report_path.write_text(report_html, encoding="utf-8")

    return report_path
