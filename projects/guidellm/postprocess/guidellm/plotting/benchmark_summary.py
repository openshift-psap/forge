"""Generate throughput vs concurrency chart (benchmark summary)."""

from __future__ import annotations

from pathlib import Path

from projects.caliper.engine.parameter_matrix import (
    create_legend_name,
    create_legend_title,
    get_varying_parameters,
)
from projects.caliper.postprocess.helpers.visualization_utils import write_full_page_html


def generate_benchmark_summary(records: list, output_dir: Path) -> str | None:
    """Generate mean throughput vs concurrency chart."""
    try:
        import plotly.graph_objects as go

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
            tokens_per_second = record.metrics.get("tokens_per_second", 0)

            if legend_name not in data_groups:
                data_groups[legend_name] = {"concurrency": [], "tokens_per_second": []}

            data_groups[legend_name]["concurrency"].append(concurrency)
            data_groups[legend_name]["tokens_per_second"].append(tokens_per_second)

        # Create figure
        fig = go.Figure()

        def _numeric_sort_key(item):
            name = item[0]
            try:
                return (0, float(name))
            except (ValueError, TypeError):
                return (1, name)

        # Add traces for each group
        colors = ["blue", "red", "green", "purple", "orange", "brown", "pink", "gray"]
        for i, (legend_name, data) in enumerate(sorted(data_groups.items(), key=_numeric_sort_key)):
            color = colors[i % len(colors)]

            fig.add_trace(
                go.Scatter(
                    x=data["concurrency"],
                    y=data["tokens_per_second"],
                    mode="markers+lines",
                    name=legend_name,
                    marker={"color": color, "size": 8},
                    line={"color": color, "width": 2},
                )
            )

        # Update layout
        legend_title = create_legend_title(varying_params)
        fig.update_layout(
            title="Throughput vs Concurrency",
            xaxis_title="Concurrency Level",
            yaxis_title="Tokens per Second",
            legend_title_text=legend_title,
            width=800,
            height=500,
        )

        # Save the plot as full-page HTML
        output_file = output_dir / "throughput_vs_concurrency.html"
        write_full_page_html(fig, str(output_file), "Throughput vs Concurrency")

        return str(output_file)

    except ImportError:
        return None
    except Exception as e:
        import logging

        logging.warning(f"Failed to generate throughput chart: {e}")
        return None
