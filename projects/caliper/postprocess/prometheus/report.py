from __future__ import annotations

import html as html_module
from dataclasses import dataclass
from pathlib import Path

from projects.caliper.postprocess.helpers.visualization_utils import (
    create_report_filename,
    create_report_title_display,
    save_figure,
)
from projects.caliper.postprocess.prometheus.parser import CaptureGroupResult, MetricTimeSeries
from projects.caliper.postprocess.prometheus.plotting import (
    MAX_LABEL_VALUE_LEN,
    SUPPRESSED_LABEL_KEYS,
    build_legend_info,
    create_metric_figure,
)


@dataclass(frozen=True)
class EmbeddableMetricPlot:
    png_path: Path | None
    html_path: Path | None
    summary_text: str


def generate_group_report(
    group_result: CaptureGroupResult,
    output_dir: str | Path,
    report_number: int | None = None,
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_title = f"Prometheus Metrics: {group_result.group_name}"
    display_title = create_report_title_display(report_title, report_number)

    report_dir_name = report_title.lower().replace(" ", "_").replace(":", "").replace("-", "_")
    if report_number is not None:
        report_dir_name = f"report_{report_number:02d}_{report_dir_name}"
    report_dir = output_dir / report_dir_name
    report_dir.mkdir(parents=True, exist_ok=True)

    plot_sections: list[str] = []

    if group_result.variables:
        plot_sections.append(_render_variables_section(group_result.variables))

    for idx, metric in enumerate(group_result.metrics):
        section_html = _render_metric_section(metric, report_dir, report_dir_name, idx)
        plot_sections.append(section_html)

    report_html = _assemble_report(display_title, plot_sections)
    report_filename = create_report_filename(report_dir_name, report_number, report_title, "html")
    report_path = output_dir / report_filename
    report_path.write_text(report_html, encoding="utf-8")

    return report_path


def render_embeddable(
    metric: MetricTimeSeries,
    output_dir: str | Path,
) -> EmbeddableMetricPlot:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    unit_suffix = f" ({metric.unit})" if metric.unit else ""
    summary_text = f"{metric.description}{unit_suffix}"

    if not metric.series:
        return EmbeddableMetricPlot(png_path=None, html_path=None, summary_text=summary_text)

    fig = create_metric_figure(metric)

    png_result = save_figure(fig, output_dir, metric.metric_name, as_image=True)
    html_result = save_figure(fig, output_dir, metric.metric_name, as_image=False)

    return EmbeddableMetricPlot(
        png_path=Path(png_result) if png_result else None,
        html_path=Path(html_result) if html_result else None,
        summary_text=summary_text,
    )


def _render_metric_section(
    metric: MetricTimeSeries,
    report_dir: Path,
    report_dir_name: str,
    idx: int,
) -> str:
    escaped_desc = html_module.escape(metric.description)
    escaped_unit = html_module.escape(metric.unit)
    unit_display = f" ({escaped_unit})" if escaped_unit else ""
    title = f"{escaped_desc}{unit_display}"

    if not metric.series:
        return (
            f'<div class="metric-section">'
            f"<h2>{title}</h2>"
            f'<p class="no-data">No data available for this metric.</p>'
            f"</div>"
        )

    fig = create_metric_figure(metric)
    legend = build_legend_info(metric)
    filename = f"{idx:03d}__{metric.metric_name}"

    save_figure(fig, report_dir, filename, as_image=True)
    html_path = save_figure(fig, report_dir, filename, as_image=False)

    inline_plot = fig.to_html(include_plotlyjs=False, full_html=False)

    link_html = ""
    if html_path:
        html_rel = f"{report_dir_name}/{Path(html_path).name}"
        link_html = f'<p><a href="{html_rel}">Open interactive plot</a></p>'

    legend_html = _render_legend_html(legend)

    query_html = ""
    if metric.query:
        escaped_query = html_module.escape(metric.query.strip())
        query_html = f'<p class="query">Query: <code>{escaped_query}</code></p>'

    return f'<div class="metric-section"><h2>{title}</h2>{query_html}{link_html}{inline_plot}{legend_html}</div>'


def _render_variables_section(variables: dict[str, str]) -> str:
    rows = "\n".join(
        f"<tr><td><b>{html_module.escape(k)}</b></td><td>{html_module.escape(v)}</td></tr>"
        for k, v in sorted(variables.items())
    )
    return (
        f'<div class="variables-section">'
        f"<h2>Capture Parameters</h2>"
        f'<table class="variables-table">'
        f"<tr><th>Variable</th><th>Value</th></tr>"
        f"{rows}"
        f"</table>"
        f"</div>"
    )


def _render_legend_html(legend) -> str:
    detail_parts = []

    if legend.common_labels:
        common_items = ", ".join(
            f"<b>{html_module.escape(k)}</b>={html_module.escape(v)}"
            for k, v in sorted(legend.common_labels.items())
        )
        detail_parts.append(f'<p class="legend-common">Common labels: {common_items}</p>')

    if legend.entries and any(e.full_labels for e in legend.entries):
        rows = []
        for entry in legend.entries:
            if not entry.full_labels:
                continue
            distinguishing = {
                k: v
                for k, v in entry.full_labels.items()
                if k not in legend.common_labels
                and k not in SUPPRESSED_LABEL_KEYS
                and len(v) <= MAX_LABEL_VALUE_LEN
            }
            if not distinguishing:
                continue
            full_text = ", ".join(
                f"{html_module.escape(k)}={html_module.escape(v)}"
                for k, v in sorted(distinguishing.items())
            )
            rows.append(
                f"<tr>"
                f'<td class="legend-name">{html_module.escape(entry.short_name)}</td>'
                f"<td>{full_text}</td>"
                f"</tr>"
            )

        if rows:
            rows_html = "\n".join(rows)
            detail_parts.append(
                f'<table class="legend-table">'
                f"<tr><th>Legend</th><th>Labels</th></tr>"
                f"{rows_html}"
                f"</table>"
            )

    if not detail_parts:
        return ""

    series_count = len(legend.entries)
    inner = "\n".join(detail_parts)
    return f"<details><summary>Legend details ({series_count} series)</summary>{inner}</details>"


def _assemble_report(display_title: str, sections: list[str]) -> str:
    sections_joined = "\n".join(sections)
    escaped_title = html_module.escape(display_title)
    return (
        f"<!DOCTYPE html>\n"
        f"<html>\n"
        f"<head>\n"
        f'<meta charset="utf-8">\n'
        f"<title>{escaped_title}</title>\n"
        f'<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>\n'
        f"<style>\n"
        f"body {{ font-family: sans-serif; margin: 20px; }}\n"
        f".metric-section {{ margin-bottom: 40px; border-bottom: 1px solid #ccc; padding-bottom: 20px; }}\n"
        f".no-data {{ color: #888; font-style: italic; }}\n"
        f".legend-common {{ color: #555; font-size: 0.9em; margin-top: 8px; }}\n"
        f".legend-table {{ border-collapse: collapse; font-size: 0.85em; margin-top: 8px; }}\n"
        f".legend-table th, .legend-table td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; }}\n"
        f".legend-table th {{ background: #f5f5f5; }}\n"
        f".legend-name {{ font-weight: bold; white-space: nowrap; }}\n"
        f".variables-section {{ margin-bottom: 30px; }}\n"
        f".variables-table {{ border-collapse: collapse; font-size: 0.9em; }}\n"
        f".variables-table th, .variables-table td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; }}\n"
        f".variables-table th {{ background: #f5f5f5; }}\n"
        f".query {{ color: #555; font-size: 0.9em; }}\n"
        f".query code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }}\n"
        f"</style>\n"
        f"</head>\n"
        f"<body>\n"
        f"<h1>{escaped_title}</h1>\n"
        f"{sections_joined}\n"
        f"</body>\n"
        f"</html>"
    )
