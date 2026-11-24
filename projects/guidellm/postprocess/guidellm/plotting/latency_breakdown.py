"""Generate latency breakdown visualization."""

from __future__ import annotations

from pathlib import Path

from projects.caliper.engine.parameter_matrix import (
    create_legend_name,
    create_legend_title,
    get_varying_parameters,
)
from projects.caliper.postprocess.helpers.visualization_utils import write_full_page_html


def generate_latency_breakdown(records: list, output_dir: Path) -> str | None:
    """Generate latency breakdown visualization."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        # Get parameters that vary across all records for legend names
        varying_params = get_varying_parameters(records)

        # Group records by their distinguishing characteristics
        data_groups = {}
        for record in records:
            # Create a legend name using only varying parameters
            legend_name = create_legend_name(record, varying_params)

            concurrency = record.metrics.get(
                "request_concurrency", record.distinguishing_labels.get("concurrency", 1)
            )

            # Extract latency metrics (convert to ms for better readability)
            ttft_median = record.metrics.get("ttft_median", 0) * 1000  # Convert to ms
            itl_median = record.metrics.get("itl_median", 0) * 1000  # Convert to ms
            request_latency_p95 = (
                record.metrics.get("request_latency_p95", 0) * 1000
            )  # Convert to ms

            if legend_name not in data_groups:
                data_groups[legend_name] = {
                    "concurrency": [],
                    "ttft_median": [],
                    "itl_median": [],
                    "request_latency_p95": [],
                }

            data_groups[legend_name]["concurrency"].append(concurrency)
            data_groups[legend_name]["ttft_median"].append(ttft_median)
            data_groups[legend_name]["itl_median"].append(itl_median)
            data_groups[legend_name]["request_latency_p95"].append(request_latency_p95)

        # Create subplots
        fig = make_subplots(
            rows=2,
            cols=2,
            subplot_titles=[
                "Time to First Token (TTFT)",
                "Inter-Token Latency (ITL)",
                "Request Latency P95",
                "Latency Comparison",
            ],
        )

        colors = ["blue", "red", "green", "purple", "orange", "brown", "pink", "gray"]

        def _numeric_sort_key(item):
            name = item[0]
            try:
                return (0, float(name))
            except (ValueError, TypeError):
                return (1, name)

        for i, (legend_name, data) in enumerate(sorted(data_groups.items(), key=_numeric_sort_key)):
            color = colors[i % len(colors)]

            # TTFT plot
            fig.add_trace(
                go.Scatter(
                    x=data["concurrency"],
                    y=data["ttft_median"],
                    mode="markers+lines",
                    name=legend_name,
                    marker={"color": color},
                    showlegend=True,
                ),
                row=1,
                col=1,
            )

            # ITL plot
            fig.add_trace(
                go.Scatter(
                    x=data["concurrency"],
                    y=data["itl_median"],
                    mode="markers+lines",
                    name=f"{legend_name} ITL",
                    marker={"color": color},
                    showlegend=False,
                ),
                row=1,
                col=2,
            )

            # Request latency P95 plot
            fig.add_trace(
                go.Scatter(
                    x=data["concurrency"],
                    y=data["request_latency_p95"],
                    mode="markers+lines",
                    name=f"{legend_name} Req P95",
                    marker={"color": color},
                    showlegend=False,
                ),
                row=2,
                col=1,
            )

            # Combined latency comparison
            fig.add_trace(
                go.Scatter(
                    x=data["concurrency"],
                    y=data["ttft_median"],
                    mode="lines",
                    name=legend_name,
                    line={"color": color, "dash": "solid"},
                    showlegend=False,
                ),
                row=2,
                col=2,
            )
            fig.add_trace(
                go.Scatter(
                    x=data["concurrency"],
                    y=data["itl_median"],
                    mode="lines",
                    name=legend_name,
                    line={"color": color, "dash": "dash"},
                    showlegend=False,
                ),
                row=2,
                col=2,
            )

        # Update layout
        legend_title = create_legend_title(varying_params)
        fig.update_layout(
            title="GuideLLM Latency Analysis",
            height=800,
            showlegend=True,
            legend_title_text=legend_title,
        )

        # Update axes labels
        fig.update_xaxes(title_text="Concurrency", row=2, col=1)
        fig.update_xaxes(title_text="Concurrency", row=2, col=2)
        fig.update_yaxes(title_text="Latency (ms)", row=1, col=1)
        fig.update_yaxes(title_text="Latency (ms)", row=1, col=2)
        fig.update_yaxes(title_text="Latency (ms)", row=2, col=1)
        fig.update_yaxes(title_text="Latency (ms)", row=2, col=2)

        # Save the plot as full-page HTML
        output_file = output_dir / "latency_breakdown.html"
        write_full_page_html(fig, str(output_file), "Latency Breakdown")

        return str(output_file)

    except ImportError:
        return None
    except Exception as e:
        import logging

        logging.warning(f"Failed to generate latency breakdown chart: {e}")
        return None
