"""GuideLLM Request Status Report - stacked bar chart of successful vs errored requests."""

from __future__ import annotations

import logging
import re  # noqa: F401
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import UnifiedResultRecord
from projects.caliper.engine.parameter_matrix import (
    create_legend_name,
    get_varying_parameters,
)
from projects.caliper.postprocess.helpers.visualization_utils import (
    create_report_filename,
    create_report_title_display,
    save_figure,
)

logger = logging.getLogger(__name__)


def _extract_status_data(
    records: list[UnifiedResultRecord],
) -> list[dict[str, Any]]:
    """Extract per-configuration request status data from records."""
    varying_params = get_varying_parameters(records)
    result = []

    for record in records:
        if not record.run_identity.get("guidellm") or record.metrics.get("no_benchmarks_found"):
            continue

        curves = record.metrics.get("performance_curves", {})
        concurrency_list = curves.get("intended_concurrency", [])
        successful_list = curves.get("successful_requests", [])
        raw_breakdown = record.metrics.get("error_breakdown", [])
        error_breakdown_list = [_normalize_breakdown(pt) for pt in raw_breakdown]

        if not concurrency_list:
            continue

        legend_name = create_legend_name(record, varying_params)

        order = sorted(range(len(concurrency_list)), key=lambda i: concurrency_list[i])
        result.append(
            {
                "legend_name": legend_name,
                "concurrency": [concurrency_list[i] for i in order],
                "successful": [successful_list[i] for i in order] if successful_list else [],
                "error_breakdown": (
                    [error_breakdown_list[i] for i in order] if error_breakdown_list else []
                ),
            }
        )

    return result


def create_successful_requests_plot(records: list[UnifiedResultRecord], title_context: str = ""):
    """Create a bar chart of successful requests per concurrency level, one color per config."""
    try:
        import plotly.graph_objects as go

        groups = _extract_status_data(records)
        if not groups:
            return None

        fig = go.Figure()

        for group in groups:
            legend_name = group["legend_name"]
            x_labels = [str(c) for c in group["concurrency"]]
            fig.add_trace(
                go.Bar(
                    x=x_labels,
                    y=group["successful"],
                    name=legend_name,
                    hovertemplate=(
                        f"<b>{legend_name}</b><br>"
                        "Concurrency: %{x}<br>"
                        "Successful: %{y}<extra></extra>"
                    ),
                )
            )

        fig.update_layout(
            title=f"Successful Requests by Concurrency{title_context}",
            xaxis_title="Concurrency Level",
            yaxis_title="Successful Requests",
            barmode="group",
            showlegend=True,
            width=1200,
            height=650,
            font={"size": 12},
        )
        fig.update_yaxes(rangemode="tozero")
        return fig

    except Exception as e:
        logger.warning("Failed to create successful requests plot: %s", e)
        return None


def create_total_errors_plot(records: list[UnifiedResultRecord], title_context: str = ""):
    """Create a bar chart of total error count per concurrency level, one color per config."""
    try:
        import plotly.graph_objects as go

        groups = _extract_status_data(records)
        if not groups:
            return None

        has_any_errors = False
        fig = go.Figure()

        for group in groups:
            legend_name = group["legend_name"]
            x_labels = [str(c) for c in group["concurrency"]]
            totals = [sum(pt.values()) for pt in group["error_breakdown"]]
            if any(t > 0 for t in totals):
                has_any_errors = True
            fig.add_trace(
                go.Bar(
                    x=x_labels,
                    y=totals,
                    name=legend_name,
                    hovertemplate=(
                        f"<b>{legend_name}</b><br>"
                        "Concurrency: %{x}<br>"
                        "Errors: %{y}<extra></extra>"
                    ),
                )
            )

        if not has_any_errors:
            return None

        fig.update_layout(
            title=f"Total Errors by Concurrency{title_context}",
            xaxis_title="Concurrency Level",
            yaxis_title="Total Errors",
            barmode="group",
            showlegend=True,
            width=1200,
            height=650,
            font={"size": 12},
        )
        fig.update_yaxes(rangemode="tozero")
        return fig

    except Exception as e:
        logger.warning("Failed to create total errors plot: %s", e)
        return None


def create_error_breakdown_plot(status_entry: dict[str, Any], title_context: str = ""):
    """Create a stacked bar chart of error categories for a single test configuration."""
    try:
        import plotly.graph_objects as go

        concurrency = status_entry["concurrency"]
        error_breakdown = status_entry["error_breakdown"]

        all_errors: dict[str, list[int]] = {}
        for i, point_errors in enumerate(error_breakdown):
            for error_msg, count in point_errors.items():
                if error_msg not in all_errors:
                    all_errors[error_msg] = [0] * len(concurrency)
                all_errors[error_msg][i] = count

        if not all_errors:
            return None

        fig = go.Figure()
        x_labels = [str(c) for c in concurrency]

        for error_msg, counts in sorted(all_errors.items()):
            short_label = _shorten_error(error_msg)
            fig.add_trace(
                go.Bar(
                    x=x_labels,
                    y=counts,
                    name=short_label,
                    hovertemplate=(
                        "Concurrency: %{x}<br>"
                        f"Error: {short_label}<br>"
                        "Count: %{y}<extra></extra>"
                    ),
                )
            )

        legend_name = status_entry["legend_name"]
        fig.update_layout(
            title=f"Errors — {legend_name}{title_context}",
            xaxis_title="Concurrency Level",
            yaxis_title="Error Count",
            barmode="stack",
            showlegend=True,
            width=1200,
            height=500,
            font={"size": 12},
        )
        fig.update_yaxes(rangemode="tozero")
        return fig

    except Exception as e:
        logger.warning("Failed to create error breakdown plot: %s", e)
        return None


_NORMALIZE_ERROR_RE = re.compile(r"(\w+Error)\(['\"][\d]+['\"]\)")


def _normalize_error(error_msg: str) -> str:
    """Collapse error repr strings that differ only by a numeric argument.

    ``LocalProtocolError('2371')`` and ``LocalProtocolError('999')`` both
    become ``LocalProtocolError(<number>)``.
    """
    return _NORMALIZE_ERROR_RE.sub(r"\1(<number>)", error_msg)


def _normalize_breakdown(point_errors: dict[str, int]) -> dict[str, int]:
    """Normalize error keys in a single breakdown dict, merging collapsed duplicates."""
    merged: dict[str, int] = {}
    for msg, count in point_errors.items():
        key = _normalize_error(msg)
        merged[key] = merged.get(key, 0) + count
    return merged


def _shorten_error(error_msg: str) -> str:
    """Shorten a repr(exception) string for display in legends."""
    if len(error_msg) <= 80:
        return error_msg
    return error_msg[:77] + "..."


def _build_error_table_html(status_entry: dict[str, Any]) -> str:
    """Build an HTML table of untruncated error messages for a single config, by concurrency."""
    import html as html_mod

    rows: list[tuple[str, int, str]] = []
    for idx, point_errors in enumerate(status_entry["error_breakdown"]):
        concurrency = (
            status_entry["concurrency"][idx] if idx < len(status_entry["concurrency"]) else "?"
        )
        for error_msg, count in point_errors.items():
            rows.append((str(concurrency), count, error_msg))

    if not rows:
        return ""

    rows.sort(key=lambda r: (-r[1], r[0]))

    total_errors = sum(r[1] for r in rows)
    html_rows = []
    for concurrency, count, error_msg in rows:
        escaped_error = html_mod.escape(error_msg)
        html_rows.append(
            f"<tr><td>{concurrency}</td><td>{count}</td><td><code>{escaped_error}</code></td></tr>"
        )

    table = (
        '<table class="error-table">'
        "<thead><tr><th>Concurrency</th>"
        "<th>Count</th><th>Error Message</th></tr></thead>"
        "<tbody>" + "".join(html_rows) + "</tbody></table>"
    )

    return (
        f"<details><summary>Error Details ({total_errors} total errors)</summary>"
        f"\n{table}\n</details>"
    )


def generate_request_status_report(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    report_number: int | None = None,
    report_title: str = "GuideLLM Request Status",
) -> str | None:
    """Generate a request status report: successful requests plot, then per-config error plots."""
    try:
        display_title = create_report_title_display(report_title, report_number)
        logger.info("Generating %s ...", display_title)

        guidellm_records = [
            r
            for r in records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]
        if not guidellm_records:
            return None

        report_dir_name = (
            f"report_{report_number:02d}_{report_title.lower().replace(' ', '_').replace('-', '_')}"
            if report_number is not None
            else "request_status"
        )
        report_dir = output_dir / report_dir_name
        report_dir.mkdir(exist_ok=True)

        from .performance_analysis import embed_plot_for_report

        sections: list[str] = []

        # --- Successful requests plot (all configs together) ---
        success_fig = create_successful_requests_plot(guidellm_records, title_context)
        if success_fig is not None:
            success_dir = report_dir / "successful"
            success_dir.mkdir(exist_ok=True)
            png_path = save_figure(success_fig, success_dir, "successful_requests", as_image=True)
            html_path = save_figure(success_fig, success_dir, "successful_requests", as_image=False)
            if html_path:
                png_rel = (
                    f"{report_dir_name}/successful/{Path(png_path).name}" if png_path else None
                )
                html_rel = f"{report_dir_name}/successful/{Path(html_path).name}"
                plot_content = embed_plot_for_report(
                    png_rel, html_rel, "Successful Requests", output_dir
                )
                sections.append(f'<div class="plot-section">\n{plot_content}\n</div>')

        # --- Total errors plot (all configs together) ---
        errors_fig = create_total_errors_plot(guidellm_records, title_context)
        if errors_fig is not None:
            errors_dir = report_dir / "total_errors"
            errors_dir.mkdir(exist_ok=True)
            png_path = save_figure(errors_fig, errors_dir, "total_errors", as_image=True)
            html_path = save_figure(errors_fig, errors_dir, "total_errors", as_image=False)
            if html_path:
                png_rel = (
                    f"{report_dir_name}/total_errors/{Path(png_path).name}" if png_path else None
                )
                html_rel = f"{report_dir_name}/total_errors/{Path(html_path).name}"
                plot_content = embed_plot_for_report(png_rel, html_rel, "Total Errors", output_dir)
                sections.append(f'<div class="plot-section">\n{plot_content}\n</div>')

        # --- Per-config error plots + tables ---
        status_data = _extract_status_data(guidellm_records)
        for entry_idx, entry in enumerate(status_data):
            legend_name = entry["legend_name"]
            error_fig = create_error_breakdown_plot(entry, title_context)
            if error_fig is None:
                continue

            entry_dir = report_dir / f"errors_{entry_idx:03d}"
            entry_dir.mkdir(exist_ok=True)
            png_path = save_figure(error_fig, entry_dir, "error_breakdown", as_image=True)
            html_path = save_figure(error_fig, entry_dir, "error_breakdown", as_image=False)
            if not html_path:
                continue

            png_rel = (
                f"{report_dir_name}/errors_{entry_idx:03d}/{Path(png_path).name}"
                if png_path
                else None
            )
            html_rel = f"{report_dir_name}/errors_{entry_idx:03d}/{Path(html_path).name}"
            plot_content = embed_plot_for_report(png_rel, html_rel, legend_name, output_dir)

            error_table = _build_error_table_html(entry)

            sections.append(
                f'<h2 id="errors-{entry_idx}">{legend_name}</h2>\n'
                f'<div class="plot-section">\n{plot_content}\n</div>\n'
                f'<div class="table-section">\n{error_table}\n</div>'
            )

        if not sections:
            return None

        main_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{display_title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f8f9fa;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 30px;
            text-align: center;
        }}
        .plot-section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        h2 {{
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }}
        .table-section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        details {{
            cursor: pointer;
        }}
        details summary {{
            font-weight: 600;
            color: #555;
            margin-bottom: 10px;
        }}
        .error-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        .error-table th, .error-table td {{
            border: 1px solid #ddd;
            padding: 8px 12px;
            text-align: left;
        }}
        .error-table th {{
            background-color: #f0f0f0;
            font-weight: 600;
        }}
        .error-table td:nth-child(2) {{
            text-align: center;
            font-weight: 600;
        }}
        .error-table code {{
            word-break: break-all;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{display_title}</h1>
        <div>Successful and errored request counts by concurrency level</div>
    </div>
    {"".join(sections)}
</body>
</html>"""

        main_filename = create_report_filename(
            report_title.lower().replace(" ", "_"), report_number, report_title, "html"
        )
        main_path = output_dir / main_filename
        main_path.write_text(main_html, encoding="utf-8")

        logger.info("Report saved as: %s", main_filename)
        return str(main_path)

    except Exception:
        logger.exception("Failed to generate request status report")
        raise
