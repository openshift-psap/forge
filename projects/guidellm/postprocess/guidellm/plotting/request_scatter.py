"""GuideLLM Request Scatter Report - per-request latency over time colored by concurrency."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import UnifiedResultRecord
from projects.caliper.engine.parameter_matrix import (
    create_legend_name,
    get_varying_parameters,
)
from projects.caliper.postprocess.helpers.visualization_utils import (
    compute_report_stem,
    create_report_filename,
    create_report_title_display,
    save_figure,
)

logger = logging.getLogger(__name__)


def _extract_scatter_data(
    records: list[UnifiedResultRecord],
) -> list[dict[str, Any]]:
    """Extract per-configuration scatter data from records."""
    varying_params = get_varying_parameters(records)
    result = []

    for record in records:
        if not record.run_identity.get("guidellm") or record.metrics.get("no_benchmarks_found"):
            continue

        scatter_list = record.metrics.get("request_scatter", [])
        if not scatter_list:
            continue

        legend_name = create_legend_name(record, varying_params)
        result.append(
            {
                "legend_name": legend_name,
                "scatter": scatter_list,
            }
        )

    return result


def create_scatter_plot(scatter_entry: dict[str, Any], title_context: str = ""):
    """Create a scatter plot of request latency over time, colored by concurrency level."""
    try:
        import plotly.graph_objects as go

        scatter_list = scatter_entry["scatter"]
        if not scatter_list:
            return None

        fig = go.Figure()

        for point in sorted(scatter_list, key=lambda p: p.get("concurrency") or 0):
            concurrency = point.get("concurrency")
            start_times = point.get("start_times", [])
            latencies = point.get("latencies", [])
            if not start_times:
                continue

            fig.add_trace(
                go.Scattergl(
                    x=start_times,
                    y=latencies,
                    mode="markers",
                    name=f"concurrency={concurrency}",
                    marker={"size": 3, "opacity": 0.5},
                    hovertemplate=(
                        f"<b>concurrency={concurrency}</b><br>"
                        "Time: %{x:.1f}s<br>"
                        "Latency: %{y:.3f}s<extra></extra>"
                    ),
                )
            )

        legend_name = scatter_entry["legend_name"]
        fig.update_layout(
            title=f"Request Latency Over Time — {legend_name}{title_context}",
            xaxis_title="Elapsed Time (s)",
            yaxis_title="Request Latency (s)",
            showlegend=True,
            width=1400,
            height=700,
            font={"size": 12},
        )
        fig.update_yaxes(rangemode="tozero")
        return fig

    except Exception as e:
        logger.warning("Failed to create scatter plot: %s", e)
        return None


def create_token_scatter_plot(scatter_entry: dict[str, Any], title_context: str = ""):
    """Create a scatter plot of output tokens per response over time, colored by concurrency."""
    try:
        import plotly.graph_objects as go

        scatter_list = scatter_entry["scatter"]
        fig = go.Figure()
        has_tokens = False

        for point in sorted(scatter_list, key=lambda p: p.get("concurrency") or 0):
            concurrency = point.get("concurrency")
            start_times = point.get("start_times", [])
            output_tokens = point.get("output_tokens", [])
            prompt_tokens = point.get("prompt_tokens", [])
            if not output_tokens:
                continue

            times, tokens, prompts = [], [], []
            for idx, token_count in enumerate(output_tokens):
                if token_count is None or idx >= len(start_times):
                    continue
                times.append(start_times[idx])
                tokens.append(token_count)
                prompts.append(prompt_tokens[idx] if idx < len(prompt_tokens) else "")
            if not times:
                continue

            has_tokens = True
            fig.add_trace(
                go.Scattergl(
                    x=times,
                    y=tokens,
                    mode="markers",
                    name=f"concurrency={concurrency}",
                    marker={"size": 3, "opacity": 0.5},
                    customdata=prompts,
                    hovertemplate=(
                        f"<b>concurrency={concurrency}</b><br>"
                        "Time: %{x:.1f}s<br>"
                        "Output tokens: %{y}<br>"
                        "Prompt tokens: %{customdata}<extra></extra>"
                    ),
                )
            )

        if not has_tokens:
            return None

        legend_name = scatter_entry["legend_name"]
        fig.update_layout(
            title=f"Output Tokens Per Response Over Time — {legend_name}{title_context}",
            xaxis_title="Elapsed Time (s)",
            yaxis_title="Output Tokens Per Response",
            showlegend=True,
            width=1400,
            height=700,
            font={"size": 12},
        )
        fig.update_yaxes(rangemode="tozero")
        return fig

    except Exception as e:
        logger.warning("Failed to create token scatter plot: %s", e)
        return None


def generate_request_scatter_report(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    report_number: int | None = None,
    report_title: str = "GuideLLM Request Scatter",
) -> str | None:
    """Generate a scatter report: one latency-over-time plot per test configuration."""
    try:
        display_title = create_report_title_display(report_title)
        logger.info("Generating %s ...", display_title)

        guidellm_records = [
            r
            for r in records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]
        if not guidellm_records:
            return None

        report_dir_name = (
            compute_report_stem(report_title, report_number)
            if report_number is not None
            else "request_scatter"
        )
        report_dir = output_dir / report_dir_name
        report_dir.mkdir(exist_ok=True)

        from .performance_analysis import embed_plot_for_report

        sections: list[str] = []
        scatter_data = _extract_scatter_data(guidellm_records)

        for entry_idx, entry in enumerate(scatter_data):
            legend_name = entry["legend_name"]
            fig = create_scatter_plot(entry, title_context)
            if fig is None:
                continue

            entry_dir = report_dir / f"scatter_{entry_idx:03d}"
            entry_dir.mkdir(exist_ok=True)
            png_path = save_figure(fig, entry_dir, "request_scatter", as_image=True)
            html_path = save_figure(fig, entry_dir, "request_scatter", as_image=False)
            if not html_path:
                continue

            png_rel = (
                f"{report_dir_name}/scatter_{entry_idx:03d}/{Path(png_path).name}"
                if png_path
                else None
            )
            html_rel = f"{report_dir_name}/scatter_{entry_idx:03d}/{Path(html_path).name}"
            plot_content = embed_plot_for_report(png_rel, html_rel, legend_name, output_dir)
            section = (
                f'<h2 id="scatter-{entry_idx}">{legend_name}</h2>\n'
                f'<div class="plot-section">\n{plot_content}\n</div>'
            )

            token_fig = create_token_scatter_plot(entry, title_context)
            if token_fig is not None:
                token_png_path = save_figure(token_fig, entry_dir, "token_scatter", as_image=True)
                token_html_path = save_figure(token_fig, entry_dir, "token_scatter", as_image=False)
                if token_html_path:
                    token_png_rel = (
                        f"{report_dir_name}/scatter_{entry_idx:03d}/{Path(token_png_path).name}"
                        if token_png_path
                        else None
                    )
                    token_html_rel = (
                        f"{report_dir_name}/scatter_{entry_idx:03d}/{Path(token_html_path).name}"
                    )
                    token_plot_content = embed_plot_for_report(
                        token_png_rel, token_html_rel, legend_name, output_dir
                    )
                    section += f'\n<div class="plot-section">\n{token_plot_content}\n</div>'

            sections.append(section)

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
            max-width: 1400px;
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
    </style>
</head>
<body>
    <div class="header">
        <h1>{display_title}</h1>
        <div>Individual request latency and output tokens per response over time, colored by concurrency level</div>
    </div>
    {"".join(sections)}
</body>
</html>"""

        main_filename = create_report_filename(report_title, report_number)
        main_path = output_dir / main_filename
        main_path.write_text(main_html, encoding="utf-8")

        logger.info("Report saved as: %s", main_filename)
        return str(main_path)

    except Exception:
        logger.exception("Failed to generate request scatter report")
        raise
