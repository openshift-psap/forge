"""GuideLLM Performance Analysis - Advanced plotting and analysis functions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

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


def _safe_get_curve_value(curves: dict, metric_name: str, index: int, default: Any = 0.0) -> Any:
    """
    Safely extract a value from a performance curve at a specific index.

    Args:
        curves: Dictionary of performance curves
        metric_name: Name of the metric curve to extract from
        index: Index in the curve array
        default: Default value if extraction fails

    Returns:
        The value at the specified index, or default if not available
    """
    try:
        curve = curves.get(metric_name, [])
        if isinstance(curve, list) and 0 <= index < len(curve):
            return curve[index]
        return default
    except (IndexError, TypeError, ValueError):
        return default


def create_dataframe_from_records(records: list[UnifiedResultRecord]) -> pd.DataFrame:
    """
    Convert Caliper UnifiedResultRecord objects to pandas DataFrame for analysis.

    Args:
        records: List of unified result records from GuideLLM

    Returns:
        DataFrame with all benchmark metrics and distinguishing labels
    """
    print(f"📊 Converting {len(records)} records to analysis dataframe...")
    data = []

    # Get parameters that vary across all records for legend names
    print("🔍 Analyzing parameter variations for meaningful legend names...")
    varying_params = get_varying_parameters(records)
    if varying_params:
        print(f"   Found varying parameters: {', '.join(sorted(varying_params))}")
    else:
        print("   No varying parameters found - using default naming")

    print("📝 Processing records and extracting metrics...")
    for record in records:
        # Skip records without GuideLLM data or missing benchmarks
        if not record.run_identity.get("guidellm") or record.metrics.get("no_benchmarks_found"):
            continue

        # Create legend name using only varying parameters
        legend_name = create_legend_name(record, varying_params)

        # Check if this is curve data (new format) or scalar data (old format)
        has_curves = "performance_curves" in record.metrics
        request_rates = record.metrics.get("request_rate", [])

        if has_curves and isinstance(request_rates, list) and len(request_rates) > 0:
            # New format: expand performance curves into multiple data points
            print(
                f"   🔄 Expanding performance curves for {legend_name} ({len(request_rates)} points)"
            )
            curves = record.metrics.get("performance_curves", {})

            for i, rate in enumerate(request_rates):
                # Create one row per rate point
                row = {
                    # Identity and configuration
                    "test_configuration": legend_name,
                    "test_base_path": record.test_base_path,
                    "rate_point_index": i,
                    # All distinguishing labels as individual columns
                    **{f"label_{k}": v for k, v in record.distinguishing_labels.items()},
                    # Core performance metrics from curves
                    "strategy": record.metrics.get("strategy", "unknown"),
                    "duration": record.metrics.get("duration", 0.0),
                    "request_concurrency": _safe_get_curve_value(
                        curves,
                        "request_concurrency",
                        i,
                        record.metrics.get("request_concurrency", 1.0),
                    ),
                    "request_rate": rate,
                    "completed_requests": _safe_get_curve_value(curves, "completed_requests", i, 0),
                    "failed_requests": _safe_get_curve_value(curves, "failed_requests", i, 0),
                    # Token metrics from curves
                    "tokens_per_second": _safe_get_curve_value(curves, "tokens_per_second", i, 0.0),
                    "input_tokens_per_second": _safe_get_curve_value(
                        curves, "input_tokens_per_second", i, 0.0
                    ),
                    "output_tokens_per_second": _safe_get_curve_value(
                        curves, "output_tokens_per_second", i, 0.0
                    ),
                    "input_tokens_per_request": record.metrics.get("input_tokens_per_request", 0.0),
                    "output_tokens_per_request": record.metrics.get(
                        "output_tokens_per_request", 0.0
                    ),
                    "total_tokens_per_request": record.metrics.get("total_tokens_per_request", 0.0),
                    # Latency metrics from curves (convert seconds to ms where needed)
                    "request_latency_median_ms": _safe_get_curve_value(
                        curves, "request_latency_median", i, 0.0
                    )
                    * 1000,
                    "request_latency_p95_ms": _safe_get_curve_value(
                        curves, "request_latency_p95", i, 0.0
                    )
                    * 1000,
                    "ttft_median_ms": _safe_get_curve_value(curves, "ttft_median", i, 0.0),
                    "ttft_p10_ms": _safe_get_curve_value(curves, "ttft_p10", i, 0.0),
                    "ttft_p25_ms": _safe_get_curve_value(curves, "ttft_p25", i, 0.0),
                    "ttft_p50_ms": _safe_get_curve_value(
                        curves, "ttft_median", i, 0.0
                    ),  # p50 = median
                    "ttft_p75_ms": _safe_get_curve_value(curves, "ttft_p75", i, 0.0),
                    "ttft_p90_ms": _safe_get_curve_value(curves, "ttft_p90", i, 0.0),
                    "ttft_p95_ms": _safe_get_curve_value(curves, "ttft_p95", i, 0.0),
                    "itl_median_ms": _safe_get_curve_value(curves, "itl_median", i, 0.0),
                    "itl_p10_ms": _safe_get_curve_value(curves, "itl_p10", i, 0.0),
                    "itl_p25_ms": _safe_get_curve_value(curves, "itl_p25", i, 0.0),
                    "itl_p50_ms": _safe_get_curve_value(
                        curves, "itl_median", i, 0.0
                    ),  # p50 = median
                    "itl_p75_ms": _safe_get_curve_value(curves, "itl_p75", i, 0.0),
                    "itl_p90_ms": _safe_get_curve_value(curves, "itl_p90", i, 0.0),
                    "itl_p95_ms": _safe_get_curve_value(curves, "itl_p95", i, 0.0),
                    "tpot_median_ms": _safe_get_curve_value(curves, "tpot_median", i, 0.0),
                    "tpot_p95_ms": _safe_get_curve_value(curves, "tpot_p95", i, 0.0),
                    # Output token throughput percentiles (not in curves currently, use zeros)
                    "output_tokens_per_second_p10": 0.0,
                    "output_tokens_per_second_p25": 0.0,
                    "output_tokens_per_second_p50": _safe_get_curve_value(
                        curves, "output_tokens_per_second", i, 0.0
                    ),
                    "output_tokens_per_second_p75": 0.0,
                    "output_tokens_per_second_p90": 0.0,
                }
                data.append(row)
        else:
            # Old format: single data point per record (backward compatibility)
            print(f"   📊 Using scalar metrics for {legend_name}")
            # Extract scalar request_rate if it's a single value
            request_rate = (
                request_rates[0]
                if isinstance(request_rates, list) and len(request_rates) > 0
                else record.metrics.get("request_rate", 0.0)
            )

            row = {
                # Identity and configuration
                "test_configuration": legend_name,
                "test_base_path": record.test_base_path,
                "rate_point_index": 0,
                # All distinguishing labels as individual columns
                **{f"label_{k}": v for k, v in record.distinguishing_labels.items()},
                # Core performance metrics
                "strategy": record.metrics.get("strategy", "unknown"),
                "duration": record.metrics.get("duration", 0.0),
                "request_concurrency": record.metrics.get("request_concurrency", 1.0),
                "request_rate": request_rate,
                "completed_requests": record.metrics.get("completed_requests", 0),
                "failed_requests": record.metrics.get("failed_requests", 0),
                # Token metrics
                "tokens_per_second": record.metrics.get("tokens_per_second", 0.0),
                "input_tokens_per_second": record.metrics.get("input_tokens_per_second", 0.0),
                "output_tokens_per_second": record.metrics.get("output_tokens_per_second", 0.0),
                "input_tokens_per_request": record.metrics.get("input_tokens_per_request", 0.0),
                "output_tokens_per_request": record.metrics.get("output_tokens_per_request", 0.0),
                "total_tokens_per_request": record.metrics.get("total_tokens_per_request", 0.0),
                # Latency metrics (in ms for consistency with topsail)
                "request_latency_median_ms": record.metrics.get("request_latency_median", 0.0)
                * 1000,
                "request_latency_p95_ms": record.metrics.get("request_latency_p95", 0.0) * 1000,
                "ttft_median_ms": record.metrics.get("ttft_median", 0.0),
                "ttft_p10_ms": record.metrics.get("ttft_p10", 0.0),
                "ttft_p25_ms": record.metrics.get("ttft_p25", 0.0),
                "ttft_p50_ms": record.metrics.get("ttft_p50", 0.0),
                "ttft_p75_ms": record.metrics.get("ttft_p75", 0.0),
                "ttft_p90_ms": record.metrics.get("ttft_p90", 0.0),
                "ttft_p95_ms": record.metrics.get("ttft_p95", 0.0),
                "itl_median_ms": record.metrics.get("itl_median", 0.0),
                "itl_p10_ms": record.metrics.get("itl_p10", 0.0),
                "itl_p25_ms": record.metrics.get("itl_p25", 0.0),
                "itl_p50_ms": record.metrics.get("itl_p50", 0.0),
                "itl_p75_ms": record.metrics.get("itl_p75", 0.0),
                "itl_p90_ms": record.metrics.get("itl_p90", 0.0),
                "itl_p95_ms": record.metrics.get("itl_p95", 0.0),
                "tpot_median_ms": record.metrics.get("tpot_median", 0.0),
                "tpot_p95_ms": record.metrics.get("tpot_p95", 0.0),
                # Output token throughput percentiles
                "output_tokens_per_second_p10": record.metrics.get(
                    "output_tokens_per_second_p10", 0.0
                ),
                "output_tokens_per_second_p25": record.metrics.get(
                    "output_tokens_per_second_p25", 0.0
                ),
                "output_tokens_per_second_p50": record.metrics.get(
                    "output_tokens_per_second_p50", 0.0
                ),
                "output_tokens_per_second_p75": record.metrics.get(
                    "output_tokens_per_second_p75", 0.0
                ),
                "output_tokens_per_second_p90": record.metrics.get(
                    "output_tokens_per_second_p90", 0.0
                ),
            }
            data.append(row)

    if not data:
        print("⚠️  No GuideLLM data found in records")
        return pd.DataFrame()

    print(f"✅ Successfully processed {len(data)} data points from {len(records)} records")
    df = pd.DataFrame(data)

    # Sort for consistent ordering
    print("📋 Organizing data by configuration, concurrency, and request rate...")
    # Sort and fill any NaN values in numeric columns with 0 for consistent plotting
    df = df.sort_values(
        ["test_configuration", "request_concurrency", "request_rate", "rate_point_index"]
    )

    # Fill NaN values in numeric columns with appropriate defaults
    numeric_columns = [
        col
        for col in df.columns
        if col not in ["test_configuration", "test_base_path", "strategy"] and "label_" not in col
    ]
    for col in numeric_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Show what we found
    configs = df["test_configuration"].unique()
    total_records = len(
        [
            r
            for r in records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]
    )

    print(
        f"🎯 Expanded {total_records} benchmark records into {len(df)} data points across {len(configs)} configurations:"
    )
    for config in configs:
        config_data = df[df["test_configuration"] == config]
        rate_points = len(config_data)
        rate_range = (
            f"{config_data['request_rate'].min():.1f}-{config_data['request_rate'].max():.1f} req/s"
            if rate_points > 1
            else f"{config_data['request_rate'].iloc[0]:.1f} req/s"
        )
        print(f"   • {config}: {rate_points} rate points ({rate_range})")

    return df


# Core plotting functions that return figures
def create_throughput_scaling_plot(df: pd.DataFrame, title_context: str = ""):
    """Create throughput scaling scatter plot."""
    try:
        print("📈 Creating throughput scaling scatter plot...")
        import plotly.express as px

        if df.empty:
            print("⚠️  No data available for throughput scaling plot")
            return None

        title = f"Request Throughput vs Concurrency by Configuration{title_context}"

        fig = px.scatter(
            df,
            x="request_concurrency",
            y="request_rate",
            color="test_configuration",
            size="tokens_per_second",
            hover_data={
                "strategy": True,
                "request_latency_median_ms": ":.1f",
                "ttft_median_ms": ":.1f",
                "tokens_per_second": ":.0f",
            },
            title=title,
            labels={
                "request_concurrency": "Concurrency Level",
                "request_rate": "Request Rate (req/s)",
                "test_configuration": "Configuration",
            },
        )

        fig.update_traces(textposition="top center")
        fig.update_layout(
            showlegend=True, width=800, height=500, font={"size": 12}, title_font_size=16
        )
        fig.update_yaxes(rangemode="tozero")

        print("✅ Throughput scaling plot created successfully")
        return fig

    except Exception as e:
        print(f"❌ Failed to create throughput scaling plot: {e}")
        return None


def create_latency_vs_throughput_plot(df: pd.DataFrame, title_context: str = ""):
    """Create latency vs throughput scatter plot."""
    try:
        print("📈 Creating latency vs throughput trade-off plot...")
        import plotly.express as px

        if df.empty:
            print("⚠️  No data available for latency vs throughput plot")
            return None

        title = f"Latency vs Throughput Trade-off{title_context}"

        fig = px.scatter(
            df,
            x="request_rate",
            y="request_latency_median_ms",
            color="test_configuration",
            size="tokens_per_second",
            hover_data={"strategy": True, "request_concurrency": True, "ttft_median_ms": ":.1f"},
            title=title,
            labels={
                "request_rate": "Request Rate (req/s)",
                "request_latency_median_ms": "Latency (ms)",
                "test_configuration": "Configuration",
            },
        )

        fig.update_layout(
            showlegend=True, width=800, height=500, font={"size": 12}, title_font_size=16
        )

        print("✅ Latency vs throughput plot created successfully")
        return fig

    except Exception as e:
        print(f"❌ Failed to create latency vs throughput plot: {e}")
        return None


def create_token_throughput_vs_concurrency_plot(df: pd.DataFrame, title_context: str = ""):
    """Create token throughput vs concurrency line plot."""
    try:
        print("📈 Creating token throughput vs concurrency scaling plot...")
        import plotly.express as px

        if df.empty:
            print("⚠️  No data available for token throughput vs concurrency plot")
            return None

        title = f"Token Throughput vs Concurrency{title_context}<br><sub>Higher is better</sub>"

        fig = px.line(
            df,
            x="request_concurrency",
            y="tokens_per_second",
            color="test_configuration",
            markers=True,
            hover_data={
                "strategy": True,
                "request_rate": ":.1f",
                "ttft_median_ms": ":.1f",
                "request_latency_median_ms": ":.1f",
            },
            title=title,
            labels={
                "request_concurrency": "Concurrency Level",
                "tokens_per_second": "Tokens per Second",
                "test_configuration": "Configuration",
            },
        )

        fig.update_traces(mode="lines+markers")
        fig.update_layout(
            showlegend=True, width=800, height=500, font={"size": 12}, title_font_size=16
        )
        fig.update_yaxes(rangemode="tozero")

        print("✅ Token throughput vs concurrency plot created successfully")
        return fig

    except Exception as e:
        print(f"❌ Failed to create token throughput vs concurrency plot: {e}")
        return None


def create_ttft_analysis_plot(df: pd.DataFrame, title_context: str = ""):
    """Create TTFT analysis line plot."""
    try:
        print("📈 Creating TTFT (Time to First Token) analysis plot...")
        import plotly.express as px

        if df.empty:
            print("⚠️  No data available for TTFT analysis plot")
            return None

        title = f"TTFT vs Concurrency{title_context}<br><sub>Lower is better</sub>"

        fig = px.line(
            df,
            x="request_concurrency",
            y="ttft_median_ms",
            color="test_configuration",
            markers=True,
            hover_data={"strategy": True, "request_rate": ":.1f", "tokens_per_second": ":.0f"},
            title=title,
            labels={
                "request_concurrency": "Concurrency Level",
                "ttft_median_ms": "TTFT P50 (ms)",
                "test_configuration": "Configuration",
            },
        )

        fig.update_traces(mode="lines+markers")
        fig.update_layout(
            showlegend=True, width=800, height=500, font={"size": 12}, title_font_size=16
        )
        fig.update_yaxes(rangemode="tozero")

        print("✅ TTFT analysis plot created successfully")
        return fig

    except Exception as e:
        print(f"❌ Failed to create TTFT analysis plot: {e}")
        return None


def create_token_throughput_percentiles_plot(df: pd.DataFrame, title_context: str = ""):
    """Create token throughput percentiles plot."""
    try:
        print("📈 Creating token throughput percentiles distribution plot...")
        import plotly.express as px
        import plotly.graph_objects as go

        if df.empty:
            print("⚠️  No data available for token throughput percentiles plot")
            return None

        title = f"Output Token Throughput Percentiles{title_context}<br><sub>Higher is better</sub>"

        fig = go.Figure()

        # Get unique configurations and colors
        configurations = sorted(df["test_configuration"].unique())
        print(f"   Plotting {len(configurations)} configurations with percentile distributions...")
        available_colors = px.colors.qualitative.Set1
        color_map = {
            config: available_colors[i % len(available_colors)]
            for i, config in enumerate(configurations)
        }

        # Percentiles to plot
        percentiles = [
            ("P10", "output_tokens_per_second_p10", {"width": 2, "dash": "longdash"}, 0.6),
            ("P25", "output_tokens_per_second_p25", {"width": 2, "dash": "dot"}, 0.7),
            ("P50", "output_tokens_per_second_p50", {"width": 4, "dash": "solid"}, 1.0),
            ("P75", "output_tokens_per_second_p75", {"width": 3, "dash": "dash"}, 0.9),
            ("P90", "output_tokens_per_second_p90", {"width": 2, "dash": "dashdot"}, 0.8),
        ]
        print(f"   Adding {len(percentiles)} percentile lines per configuration...")

        for config in configurations:
            config_df = df[df["test_configuration"] == config].sort_values("request_concurrency")

            for perc_name, perc_col, line_style, opacity in percentiles:
                if perc_col in config_df.columns and not config_df[perc_col].isna().all():
                    fig.add_trace(
                        go.Scatter(
                            x=config_df["request_concurrency"],
                            y=config_df[perc_col],
                            mode="lines+markers",
                            name=f"{config} - {perc_name}",
                            line=dict(color=color_map[config], **line_style),
                            opacity=opacity,
                        )
                    )

        fig.update_layout(
            title=title,
            xaxis_title="Concurrency Level",
            yaxis_title="Output Tokens per Second",
            showlegend=True,
            width=900,
            height=600,
            font={"size": 12},
            title_font_size=16,
        )
        fig.update_yaxes(rangemode="tozero")

        print("✅ Token throughput percentiles plot created successfully")
        return fig

    except Exception as e:
        print(f"❌ Failed to create token throughput percentiles plot: {e}")
        return None


# Wrapper functions for backward compatibility
def generate_throughput_scaling_analysis(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    as_image: bool = True,
    report_number: int | None = None,
) -> str | None:
    """Generate throughput scaling analysis and save to file."""
    df = create_dataframe_from_records(records)
    if df.empty:
        return None

    fig = create_throughput_scaling_plot(df, title_context)
    if fig is None:
        return None

    return save_figure(fig, output_dir, "throughput_scaling_analysis", as_image, report_number)


def generate_latency_vs_throughput_analysis(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    as_image: bool = True,
    report_number: int | None = None,
) -> str | None:
    """Generate latency vs throughput analysis and save to file."""
    df = create_dataframe_from_records(records)
    if df.empty:
        return None

    fig = create_latency_vs_throughput_plot(df, title_context)
    if fig is None:
        return None

    return save_figure(fig, output_dir, "latency_vs_throughput_analysis", as_image, report_number)


def generate_token_throughput_vs_concurrency(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    as_image: bool = True,
    report_number: int | None = None,
) -> str | None:
    """Generate token throughput vs concurrency analysis and save to file."""
    print("\n🚀 Generating token throughput vs concurrency analysis...")
    df = create_dataframe_from_records(records)
    if df.empty:
        return None

    fig = create_token_throughput_vs_concurrency_plot(df, title_context)
    if fig is None:
        return None

    return save_figure(fig, output_dir, "token_throughput_vs_concurrency", as_image, report_number)


def generate_ttft_analysis(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    as_image: bool = True,
    report_number: int | None = None,
) -> str | None:
    """Generate TTFT analysis and save to file."""
    df = create_dataframe_from_records(records)
    if df.empty:
        return None

    fig = create_ttft_analysis_plot(df, title_context)
    if fig is None:
        return None

    return save_figure(fig, output_dir, "ttft_analysis", as_image, report_number)


def generate_token_throughput_percentiles_analysis(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    as_image: bool = True,
    report_number: int | None = None,
) -> str | None:
    """Generate token throughput percentiles analysis and save to file."""
    df = create_dataframe_from_records(records)
    if df.empty:
        return None

    fig = create_token_throughput_percentiles_plot(df, title_context)
    if fig is None:
        return None

    # Use larger size for percentiles plot by calling save_figure with custom dimensions
    return save_figure(
        fig,
        output_dir,
        "token_throughput_percentiles",
        as_image,
        report_number,
        width=900,
        height=600,
    )


def generate_comprehensive_performance_report(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    report_number: int | None = None,
    report_title: str = "GuideLLM Performance Analysis",
) -> str | None:
    """
    Generate comprehensive performance analysis report with separate plot files.

    Args:
        records: List of unified result records
        output_dir: Directory to save files
        title_context: Additional context for titles
        report_number: Optional report number for file naming (e.g., 0 for "Report 00:")
        report_title: Title for the report (used in filename and HTML title)
    """
    try:
        # Create report identifier using core utilities
        display_title = create_report_title_display(report_title, report_number)

        print(f"\n🚀 Starting {display_title} generation...")
        print("=" * 70)

        df = create_dataframe_from_records(records)
        if df.empty:
            print("❌ No data available for analysis")
            return None

        print("\n📊 Generating performance analysis plots...")
        print("   This may take a moment as we create high-quality visualizations...")

        # Generate all the plots as figures
        plot_functions = [
            ("Token Throughput vs Concurrency", create_token_throughput_vs_concurrency_plot),
            ("TTFT Analysis", create_ttft_analysis_plot),
            ("Token Throughput Percentiles", create_token_throughput_percentiles_plot),
            ("Throughput Scaling", create_throughput_scaling_plot),
            ("Latency vs Throughput", create_latency_vs_throughput_plot),
        ]

        # Create dedicated directory for this report
        if report_number is not None:
            report_dir_name = f"report_{report_number:02d}_{report_title.lower().replace(' ', '_').replace(':', '').replace('-', '_')}"
        else:
            report_dir_name = "performance_analysis"

        report_dir = output_dir / report_dir_name
        report_dir.mkdir(exist_ok=True)
        print(f"\n📁 Created report directory: {report_dir_name}")

        plots_data = []
        for i, (plot_name, plot_func) in enumerate(plot_functions, 1):
            print(f"\n📈 [{i}/{len(plot_functions)}] Processing {plot_name}...")
            try:
                fig = plot_func(df, title_context)
                if fig:
                    # Save as both PNG and HTML in the report directory
                    filename = plot_name.lower().replace(" ", "_")
                    print(f"💾 Saving {plot_name} as PNG and HTML...")

                    # Save PNG image
                    width = 900 if "Percentiles" in plot_name else 800
                    height = 600 if "Percentiles" in plot_name else 500
                    png_path = save_figure(
                        fig, report_dir, filename, as_image=True, width=width, height=height
                    )

                    # Save HTML version
                    html_path = save_figure(fig, report_dir, filename, as_image=False)

                    if png_path and html_path:
                        # Store relative paths for linking
                        plots_data.append(
                            (
                                plot_name,
                                f"{report_dir_name}/{Path(png_path).name}",  # PNG path
                                f"{report_dir_name}/{Path(html_path).name}",  # HTML path
                            )
                        )
                        print(f"✅ {plot_name} saved as PNG and HTML")
                else:
                    print(f"⚠️  {plot_name} could not be created (no figure returned)")

            except Exception as e:
                print(f"❌ Failed to generate {plot_name}: {e}")

        print(f"\n✅ Successfully generated {len(plots_data)} visualizations!")

        # Generate summary statistics
        print("\n📊 Computing performance statistics and insights...")
        summary_stats = _generate_performance_summary(df)

        if summary_stats.get("best_tokens"):
            best = summary_stats["best_tokens"]
            print(f"   🏆 Best performance: {best['value']:.0f} tok/s ({best['config']})")

        # Create comprehensive HTML report
        print("\n📝 Assembling comprehensive HTML report...")
        print("   🔗 Creating report with images linking to interactive HTML versions...")

        html_content = _create_comprehensive_html_report_with_images(
            plots_data, summary_stats, title_context, display_title
        )

        # Write report with proper naming using core utility
        filename = create_report_filename(
            "performance_analysis", report_number, report_title, "html"
        )
        report_file = output_dir / filename
        print(f"\n💾 Writing {display_title} to: {report_file.name}")
        report_file.write_text(html_content, encoding="utf-8")

        file_size_mb = report_file.stat().st_size / (1024 * 1024)
        print(f"📄 Report complete! File size: {file_size_mb:.1f}MB")
        print("=" * 70)
        print(f"🎉 {display_title} ready: {report_file.name}")

        return str(report_file)

    except Exception as e:
        print(f"❌ Failed to generate comprehensive performance report: {e}")
        return None


def _generate_performance_summary(df: pd.DataFrame) -> dict[str, Any]:
    """Generate performance summary statistics from the dataframe."""
    if df.empty:
        print("⚠️  No data available for performance summary")
        return {}

    print("   🔍 Analyzing best performers across all metrics...")

    # Find best performers
    best_tokens_idx = df["tokens_per_second"].idxmax()
    best_efficiency_idx = (
        df["tokens_per_second"] / df["request_concurrency"].replace(0, 1)
    ).idxmax()
    best_ttft_idx = df["ttft_median_ms"].idxmin()

    # Configuration analysis
    print("   📊 Computing configuration performance rankings...")
    config_performance = (
        df.groupby("test_configuration")
        .agg(
            {
                "tokens_per_second": "max",
                "ttft_median_ms": "mean",
                "request_rate": "max",
                "request_concurrency": "max",
            }
        )
        .sort_values("tokens_per_second", ascending=False)
    )

    print(f"   ✅ Performance analysis complete for {len(config_performance)} configurations")

    return {
        "total_configurations": len(df["test_configuration"].unique()),
        "total_strategies": len(df),
        "best_tokens": {
            "value": df.loc[best_tokens_idx, "tokens_per_second"],
            "config": df.loc[best_tokens_idx, "test_configuration"],
            "strategy": df.loc[best_tokens_idx, "strategy"],
            "concurrency": df.loc[best_tokens_idx, "request_concurrency"],
        },
        "best_efficiency": {
            "value": df.loc[best_efficiency_idx, "tokens_per_second"]
            / max(df.loc[best_efficiency_idx, "request_concurrency"], 1),
            "config": df.loc[best_efficiency_idx, "test_configuration"],
            "strategy": df.loc[best_efficiency_idx, "strategy"],
        },
        "best_ttft": {
            "value": df.loc[best_ttft_idx, "ttft_median_ms"],
            "config": df.loc[best_ttft_idx, "test_configuration"],
            "strategy": df.loc[best_ttft_idx, "strategy"],
        },
        "config_ranking": config_performance.to_dict("index"),
    }


def _create_comprehensive_html_report_with_images(
    plots_data: list[tuple[str, str, str]],
    summary_stats: dict[str, Any],
    title_context: str,
    display_title: str = "GuideLLM Performance Analysis",
) -> str:
    """Create comprehensive HTML performance analysis report with embedded or linked images."""

    html_parts = []

    # HTML header
    html_parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{display_title}{title_context}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f8f9fa;
            line-height: 1.6;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 30px;
            text-align: center;
        }}
        .section {{
            background: white;
            padding: 25px;
            margin-bottom: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .plot-section {{
            background: white;
            padding: 25px;
            margin-bottom: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .plot-image {{
            max-width: 100%;
            height: auto;
            border: 1px solid #ddd;
            border-radius: 5px;
            margin: 10px 0;
            cursor: pointer;
        }}
        .plot-link {{
            display: inline-block;
            margin: 10px 15px 10px 0;
            padding: 12px 20px;
            background: #007bff;
            color: white;
            text-decoration: none;
            border-radius: 5px;
        }}
        .insight {{
            background: #e9f7ef;
            padding: 15px;
            border-left: 4px solid #28a745;
            margin: 15px 0;
        }}
        .stat {{
            background: #fff3cd;
            padding: 10px 15px;
            margin: 8px 0;
            border-left: 4px solid #ffc107;
            border-radius: 0 4px 4px 0;
        }}
        .ranking {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin: 10px 0;
        }}
        h1 {{ margin: 0; font-size: 2.5em; }}
        h2 {{ color: #495057; }}
        h3 {{ color: #6c757d; margin-top: 30px; }}
        .meta {{ opacity: 0.9; margin-top: 10px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 {display_title}</h1>
        <div class="meta">Comprehensive performance analysis including token throughput scaling and latency patterns{title_context}</div>
    </div>""")

    # Analysis plots section
    if plots_data:
        for plot_name, png_path, html_path in plots_data:
            # Image linked to HTML version
            html_parts.append(f"""
    <div class="plot-section">
        <h3>📈 {plot_name}</h3>
        <a href="{html_path}" target="_blank">
            <img src="{png_path}" alt="{plot_name}" class="plot-image" title="Click to view interactive version">
        </a>
        <br>
        <small>💡 Click image to view interactive version</small>
    </div>""")

    # Footer
    html_parts.append("""
</body>
</html>""")

    return "".join(html_parts)
