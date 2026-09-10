"""GuideLLM Performance Analysis - Advanced plotting and analysis functions."""

from __future__ import annotations

import base64
import logging
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

logger = logging.getLogger(__name__)

# Plot configuration constants
PLOT_CONFIG = {
    "width": 1700,
    "height": 500,
    "font": {"size": 12},
    "title_font_size": 16,
}

PLOT_CONFIG_LARGE = {
    "width": 1700,
    "height": 600,
    "font": {"size": 12},
    "title_font_size": 16,
}


def _image_to_base64(image_path: str | Path) -> str:
    """Convert an image file to a base64 data URI.

    Args:
        image_path: Path to the image file

    Returns:
        Base64 data URI string
    """
    try:
        with open(image_path, "rb") as img_file:
            img_data = img_file.read()
            img_b64 = base64.b64encode(img_data).decode("utf-8")
            return f"data:image/png;base64,{img_b64}"
    except Exception as e:
        logger.warning(f"Failed to convert image to base64: {e}")
        return ""


def _read_html_content(html_path: str | Path) -> str:
    """Read HTML file content for embedding.

    Args:
        html_path: Path to the HTML file

    Returns:
        HTML content string
    """
    try:
        logger.info(f"🔍 Reading HTML content from: {html_path}")

        # Check if file exists
        if not Path(html_path).exists():
            logger.warning(f"❌ HTML file not found: {html_path}")
            return ""

        with open(html_path, encoding="utf-8") as html_file:
            content = html_file.read()
            content_size_kb = len(content.encode("utf-8")) / 1024
            logger.info(f"   📄 Read {content_size_kb:.1f} KB from HTML file")

            # Import re for regex operations
            import re

            # Extract body content to avoid HTML document conflicts but keep all scripts
            body_match = re.search(r"<body[^>]*>(.*?)</body>", content, re.DOTALL | re.IGNORECASE)
            if body_match:
                body_content = body_match.group(1)
                body_size_kb = len(body_content.encode("utf-8")) / 1024
                logger.info(f"   ✅ Extracted body content: {body_size_kb:.1f} KB")
                return body_content
            else:
                # If no body tags found, use full content
                logger.warning("   ⚠️  No body tags found, using full content")
                logger.info(f"   📄 Using full HTML content: {content_size_kb:.1f} KB")
                return content
    except Exception as e:
        logger.warning(f"❌ Failed to read HTML content from {html_path}: {e}")
        return ""


def _create_plot_with_optional_png_spoiler(
    html_content: str,
    png_path: str | Path | None,
    plot_name: str,
    output_dir: Path,
    html_path: str | Path | None = None,
) -> str:
    """Create HTML content with embedded plot and optional PNG spoiler.

    Args:
        html_content: HTML content to embed directly
        png_path: Relative path to PNG file (None if PNG generation failed)
        plot_name: Name of the plot for alt text
        output_dir: Base output directory to resolve PNG path
        html_path: Relative path to HTML file (for large content linking)

    Returns:
        HTML string with embedded plot and optional PNG spoiler
    """
    # Embed directly as requested
    result = f"""
        <div style="width: 100%; height: 600px; margin: 10px 0;">
            {html_content}
        </div>"""

    # Add simple link to PNG version if it exists
    if png_path is not None:
        full_png_path = Path(output_dir) / png_path
        if full_png_path.exists():
            result += f"""
        <div style="margin-top: 15px; text-align: center;">
            <a href="{png_path}" target="_blank" style="color: #007acc; text-decoration: underline; font-size: 14px;">📸 Image version</a>
        </div>"""
        else:
            logger.debug(f"PNG file not found for {plot_name} at {full_png_path}")
    else:
        logger.debug(f"PNG generation was skipped for {plot_name} (HTML-only mode)")

    return result


def embed_plot_for_report(
    png_path: str | Path | None, html_path: str | Path, plot_name: str, output_dir: Path
) -> str:
    """
    Centralized function to embed plots in reports with HTML content and optional PNG spoiler.

    This is the single point to modify if we want to change how plots are embedded across all reports.
    Currently embeds HTML directly with an optional PNG spoiler, but can be easily modified to change
    the embedding behavior project-wide.

    Args:
        png_path: Relative path to PNG file (None if PNG generation failed)
        html_path: Relative path to HTML file
        plot_name: Name of the plot for alt text
        output_dir: Base output directory to resolve paths

    Returns:
        HTML string with embedded plot content
    """
    logger.info(f"🔗 Embedding plot: {plot_name}")
    logger.info(f"   📁 Output dir: {output_dir}")
    logger.info(f"   🌐 HTML path: {html_path}")
    logger.info(f"   🖼️  PNG path: {png_path}")

    # Resolve full HTML path
    full_html_path = Path(output_dir) / html_path
    logger.info(f"   📄 Full HTML path: {full_html_path}")

    # Read HTML content for direct embedding
    plot_html_content = _read_html_content(full_html_path)

    if not plot_html_content.strip():
        logger.warning(f"   ⚠️  Empty HTML content for {plot_name}")
        return f"<p>⚠️ Could not load interactive plot for {plot_name}</p>"

    # Create plot with optional PNG spoiler
    result = _create_plot_with_optional_png_spoiler(
        plot_html_content, png_path, plot_name, output_dir, html_path
    )

    result_size_kb = len(result.encode("utf-8")) / 1024
    logger.info(f"   ✅ Successfully embedded {plot_name} ({result_size_kb:.1f} KB)")
    return result


# Filesystem-unsafe characters for path sanitization
_PATH_UNSAFE_CHARS = ["/", "\\", ":", "*", "?", "|", "<", ">", '"']


def sanitize_for_path(text: str) -> str:
    """Replace filesystem-unsafe characters in label values."""
    result = str(text)
    for char in _PATH_UNSAFE_CHARS:
        result = result.replace(char, "_")
    return result


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


def _custom_configuration_sort_key(config_name: str) -> tuple[int, str]:
    """
    Create a sort key that ensures heterogeneous configurations appear before multi-turn.

    Args:
        config_name: Configuration name from test_configuration column

    Returns:
        Tuple of (priority, config_name) for sorting
    """
    # Convert to lowercase for case-insensitive comparison
    config_lower = config_name.lower()

    # Priority: 0 = heterogeneous (first), 1 = multi-turn (second), 2 = others (last)
    if "heterogeneous" in config_lower:
        priority = 0
    elif "multi-turn" in config_lower or "multi_turn" in config_lower:
        priority = 1
    else:
        priority = 2

    return (priority, config_name)


def create_dataframe_from_records(records: list[UnifiedResultRecord]) -> pd.DataFrame:
    """
    Convert Caliper UnifiedResultRecord objects to pandas DataFrame for analysis.

    Args:
        records: List of unified result records from GuideLLM

    Returns:
        DataFrame with all benchmark metrics and distinguishing labels
    """
    logger.info(f"📊 Converting {len(records)} records to analysis dataframe...")
    data = []

    # Get parameters that vary across all records for legend names
    logger.info("🔍 Analyzing parameter variations for meaningful legend names...")
    varying_params = get_varying_parameters(records)
    if varying_params:
        logger.info(f"   Found varying parameters: {', '.join(sorted(varying_params))}")
    else:
        logger.info("   No varying parameters found - using default naming")

    logger.info("📝 Processing records and extracting metrics...")
    for record in records:
        # Skip records without GuideLLM data or missing benchmarks
        if not record.run_identity.get("guidellm") or record.metrics.get("no_benchmarks_found"):
            continue

        # Create legend name using only varying parameters
        legend_name = create_legend_name(record, varying_params)

        # Extract performance curves data
        request_rates = record.metrics.get("request_rate", [])

        if not (
            isinstance(request_rates, list)
            and len(request_rates) > 0
            and "performance_curves" in record.metrics
        ):
            # Skip records that don't have the expected curve format
            logger.info(f"   ⚠️  Skipping {legend_name} - no performance curves found")
            continue

        # Expand performance curves into multiple data points
        logger.info(
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
                "intended_concurrency": _safe_get_curve_value(
                    curves,
                    "intended_concurrency",
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
                "output_tokens_per_request": record.metrics.get("output_tokens_per_request", 0.0),
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
                "ttft_p50_ms": _safe_get_curve_value(curves, "ttft_median", i, 0.0),  # p50 = median
                "ttft_p75_ms": _safe_get_curve_value(curves, "ttft_p75", i, 0.0),
                "ttft_p90_ms": _safe_get_curve_value(curves, "ttft_p90", i, 0.0),
                "ttft_p95_ms": _safe_get_curve_value(curves, "ttft_p95", i, 0.0),
                "itl_median_ms": _safe_get_curve_value(curves, "itl_median", i, 0.0),
                "itl_p10_ms": _safe_get_curve_value(curves, "itl_p10", i, 0.0),
                "itl_p25_ms": _safe_get_curve_value(curves, "itl_p25", i, 0.0),
                "itl_p50_ms": _safe_get_curve_value(curves, "itl_median", i, 0.0),  # p50 = median
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

    if not data:
        logger.info("⚠️  No GuideLLM data found in records")
        return pd.DataFrame()

    logger.info(f"✅ Successfully processed {len(data)} data points from {len(records)} records")
    df = pd.DataFrame(data)

    # Sort for consistent ordering
    logger.info("📋 Organizing data by configuration, concurrency, and request rate...")
    # Add custom sort column to ensure heterogeneous comes before multi-turn
    df["_config_sort_key"] = df["test_configuration"].apply(
        lambda x: _custom_configuration_sort_key(x)[0]
    )

    # Sort and fill any NaN values in numeric columns with 0 for consistent plotting
    df = df.sort_values(
        [
            "_config_sort_key",
            "test_configuration",
            "intended_concurrency",
            "request_rate",
            "rate_point_index",
        ]
    )

    # Remove the temporary sort column
    df = df.drop(columns=["_config_sort_key"])

    # Fill NaN values in numeric columns with appropriate defaults
    numeric_columns = [
        col
        for col in df.columns
        if col not in ["test_configuration", "test_base_path", "strategy"] and "label_" not in col
    ]
    for col in numeric_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Show what we found - preserve custom sort order
    configs = df["test_configuration"].drop_duplicates().tolist()
    total_records = len(
        [
            r
            for r in records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]
    )

    logger.info(
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
        logger.info(f"   - {config}: {rate_points} rate points ({rate_range})")

    return df


# Core plotting functions that return figures
def create_throughput_scaling_plot(df: pd.DataFrame, title_context: str = ""):
    """Create throughput scaling scatter plot."""
    try:
        logger.info("📈 Creating throughput scaling scatter plot...")
        import plotly.express as px

        if df.empty:
            logger.info("⚠️  No data available for throughput scaling plot")
            return None

        title = f"Request Throughput vs Concurrency by Configuration{title_context}"

        # Get ordered configuration list to maintain consistent legend order
        config_order = df["test_configuration"].drop_duplicates().tolist()

        fig = px.scatter(
            df,
            x="intended_concurrency",
            y="request_rate",
            color="test_configuration",
            size="tokens_per_second",
            hover_data={
                "strategy": True,
                "request_latency_median_ms": ":.1f",
                "ttft_median_ms": ":.1f",
                "tokens_per_second": ":.0f",
                "request_concurrency": ":.1f",  # Show achieved concurrency in hover
            },
            title=title,
            labels={
                "intended_concurrency": "Concurrency Level (Requested)",
                "request_rate": "Request Rate (req/s)",
                "test_configuration": "Configuration",
                "request_concurrency": "Achieved Concurrency",
            },
            category_orders={"test_configuration": config_order},
        )

        fig.update_traces(textposition="top center")
        fig.update_layout(showlegend=True, **PLOT_CONFIG)
        fig.update_yaxes(rangemode="tozero")

        logger.info("✅ Throughput scaling plot created successfully")
        return fig

    except Exception as e:
        logger.info(f"❌ Failed to create throughput scaling plot: {e}")
        return None


def create_latency_vs_throughput_plot(df: pd.DataFrame, title_context: str = ""):
    """Create latency vs throughput scatter plot."""
    try:
        logger.info("📈 Creating latency vs throughput trade-off plot...")
        import plotly.express as px

        if df.empty:
            logger.info("⚠️  No data available for latency vs throughput plot")
            return None

        title = f"Latency vs Throughput Trade-off{title_context}"

        # Get ordered configuration list to maintain consistent legend order
        config_order = df["test_configuration"].drop_duplicates().tolist()

        fig = px.scatter(
            df,
            x="request_rate",
            y="request_latency_median_ms",
            color="test_configuration",
            size="tokens_per_second",
            hover_data={
                "strategy": True,
                "intended_concurrency": ":.1f",
                "request_concurrency": ":.1f",
                "ttft_median_ms": ":.1f",
            },
            title=title,
            labels={
                "request_rate": "Request Rate (req/s)",
                "request_latency_median_ms": "Latency (ms)",
                "test_configuration": "Configuration",
            },
            category_orders={"test_configuration": config_order},
        )

        fig.update_layout(showlegend=True, **PLOT_CONFIG)

        logger.info("✅ Latency vs throughput plot created successfully")
        return fig

    except Exception as e:
        logger.info(f"❌ Failed to create latency vs throughput plot: {e}")
        return None


def create_token_throughput_vs_concurrency_plot(df: pd.DataFrame, title_context: str = ""):
    """Create token throughput vs concurrency line plot."""
    try:
        logger.info("📈 Creating token throughput vs concurrency scaling plot...")
        import plotly.express as px

        if df.empty:
            logger.info("⚠️  No data available for token throughput vs concurrency plot")
            return None

        # Check for deployment_profile values and add to subtitle if not in legend
        subtitle_parts = ["Higher is better"]

        if "label_deployment_profile" in df.columns:
            deployment_profiles = df["label_deployment_profile"].dropna().unique()
            if len(deployment_profiles) > 0:
                # Check if deployment_profile values are already part of the legend names
                legend_values = df["test_configuration"].unique()
                profile_in_legend = any(
                    any(
                        str(profile).lower() in str(legend).lower()
                        for profile in deployment_profiles
                    )
                    for legend in legend_values
                )

                if not profile_in_legend:
                    if len(deployment_profiles) == 1:
                        subtitle_parts.append(f"Deployment Profile: {deployment_profiles[0]}")
                    else:
                        subtitle_parts.append(
                            f"Deployment Profiles: {', '.join(deployment_profiles)}"
                        )

        subtitle = " | ".join(subtitle_parts)
        title = f"Token Throughput vs Concurrency{title_context}<br><sub>{subtitle}</sub>"

        # Get ordered configuration list to maintain consistent legend order
        config_order = df["test_configuration"].drop_duplicates().tolist()

        fig = px.line(
            df,
            x="intended_concurrency",
            y="tokens_per_second",
            color="test_configuration",
            markers=True,
            hover_data={
                "strategy": True,
                "request_rate": ":.1f",
                "ttft_median_ms": ":.1f",
                "request_latency_median_ms": ":.1f",
                "request_concurrency": ":.1f",  # Show achieved concurrency in hover
            },
            title=title,
            labels={
                "intended_concurrency": "Concurrency Level (Requested)",
                "tokens_per_second": "Tokens per Second",
                "test_configuration": "Configuration",
                "request_concurrency": "Achieved Concurrency",
            },
            category_orders={"test_configuration": config_order},
        )

        fig.update_traces(mode="lines+markers")
        fig.update_layout(showlegend=True, **PLOT_CONFIG)
        fig.update_yaxes(rangemode="tozero")

        logger.info("✅ Token throughput vs concurrency plot created successfully")
        return fig

    except Exception as e:
        logger.info(f"❌ Failed to create token throughput vs concurrency plot: {e}")
        return None


def create_ttft_analysis_plot(df: pd.DataFrame, title_context: str = ""):
    """Create TTFT analysis line plot."""
    try:
        logger.info("📈 Creating TTFT (Time to First Token) analysis plot...")
        import plotly.express as px

        if df.empty:
            logger.info("⚠️  No data available for TTFT analysis plot")
            return None

        # Debug: Check TTFT data availability
        if "ttft_median_ms" not in df.columns:
            logger.warning("⚠️  TTFT column 'ttft_median_ms' not found in dataframe")
            logger.info(f"   Available columns: {list(df.columns)}")
            return None

        ttft_data = df["ttft_median_ms"]
        non_zero_count = (ttft_data > 0).sum()
        logger.info(
            f"   TTFT data: {len(ttft_data)} total points, {non_zero_count} non-zero values"
        )
        logger.info(f"   TTFT range: {ttft_data.min():.1f} - {ttft_data.max():.1f} ms")

        if ttft_data.max() == 0:
            logger.warning("⚠️  All TTFT values are zero - plot may appear empty")
        if non_zero_count < 2:
            logger.warning("⚠️  Insufficient non-zero TTFT data for meaningful plot")

        title = f"TTFT vs Concurrency{title_context}<br><sub>Lower is better</sub>"

        # Get ordered configuration list to maintain consistent legend order
        config_order = df["test_configuration"].drop_duplicates().tolist()

        fig = px.line(
            df,
            x="intended_concurrency",
            y="ttft_median_ms",
            color="test_configuration",
            markers=True,
            hover_data={
                "strategy": True,
                "request_rate": ":.1f",
                "tokens_per_second": ":.0f",
                "request_concurrency": ":.1f",  # Show achieved concurrency in hover
            },
            title=title,
            labels={
                "intended_concurrency": "Concurrency Level (Requested)",
                "ttft_median_ms": "TTFT P50 (ms)",
                "test_configuration": "Configuration",
                "request_concurrency": "Achieved Concurrency",
            },
            category_orders={"test_configuration": config_order},
        )

        fig.update_traces(mode="lines+markers")
        fig.update_layout(showlegend=True, **PLOT_CONFIG)
        fig.update_yaxes(rangemode="tozero")

        logger.info("✅ TTFT analysis plot created successfully")
        return fig

    except Exception as e:
        logger.info(f"❌ Failed to create TTFT analysis plot: {e}")
        return None


def create_token_throughput_percentiles_plot(df: pd.DataFrame, title_context: str = ""):
    """Create token throughput percentiles plot."""
    try:
        logger.info("📈 Creating token throughput percentiles distribution plot...")
        import plotly.express as px
        import plotly.graph_objects as go

        if df.empty:
            logger.info("⚠️  No data available for token throughput percentiles plot")
            return None

        # Debug: Check percentile data availability
        percentile_cols = [
            "output_tokens_per_second_p10",
            "output_tokens_per_second_p25",
            "output_tokens_per_second_p50",
            "output_tokens_per_second_p75",
            "output_tokens_per_second_p90",
        ]
        missing_cols = [col for col in percentile_cols if col not in df.columns]
        if missing_cols:
            logger.warning(f"⚠️  Missing percentile columns: {missing_cols}")

        available_cols = [col for col in percentile_cols if col in df.columns]
        if not available_cols:
            logger.warning("⚠️  No percentile columns found - cannot create percentiles plot")
            return None

        logger.info(
            f"   Available percentile columns: {len(available_cols)}/{len(percentile_cols)}"
        )

        # Check if any percentile data is non-zero
        has_data = False
        for col in available_cols:
            if col in df.columns and (df[col] > 0).any():
                has_data = True
                break

        if not has_data:
            logger.warning("⚠️  All percentile values are zero - plot may appear empty")

        title = f"Output Token Throughput Percentiles{title_context}<br><sub>Higher is better</sub>"

        fig = go.Figure()

        # Get unique configurations and colors - maintain custom sort order
        configurations = df["test_configuration"].drop_duplicates().tolist()
        logger.info(
            f"   Plotting {len(configurations)} configurations with percentile distributions..."
        )
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
        logger.info(f"   Adding {len(percentiles)} percentile lines per configuration...")

        for config in configurations:
            config_df = df[df["test_configuration"] == config].sort_values("intended_concurrency")

            for perc_name, perc_col, line_style, opacity in percentiles:
                if perc_col in config_df.columns and not config_df[perc_col].isna().all():
                    fig.add_trace(
                        go.Scatter(
                            x=config_df["intended_concurrency"],
                            y=config_df[perc_col],
                            mode="lines+markers",
                            name=f"{config} - {perc_name}",
                            line=dict(color=color_map[config], **line_style),
                            opacity=opacity,
                        )
                    )

        fig.update_layout(
            title=title,
            xaxis_title="Concurrency Level (Requested)",
            yaxis_title="Output Tokens per Second",
            showlegend=True,
            **PLOT_CONFIG_LARGE,
        )
        fig.update_yaxes(rangemode="tozero")

        logger.info("✅ Token throughput percentiles plot created successfully")
        return fig

    except Exception as e:
        logger.info(f"❌ Failed to create token throughput percentiles plot: {e}")
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
    logger.info("\n🚀 Generating token throughput vs concurrency analysis...")
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
        width=PLOT_CONFIG_LARGE["width"],
        height=PLOT_CONFIG_LARGE["height"],
    )


def generate_deployment_profile_report(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    report_number: int | None = None,
    report_title: str = "GuideLLM Deployment Profile Analysis",
) -> str | None:
    """
    Generate performance analysis report with separate plots for comparison groups.

    Groups records by identical test conditions (all labels except the version key) and only
    creates plots for groups that have records with different version values.
    This enables comparing different versions under identical test conditions.

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

        logger.info(f"\n🚀 Starting {display_title} generation...")
        logger.info("=" * 70)

        # Use version as the comparison key to distinguish results
        comparison_keys = {"version"}

        # First, analyze what keys are actually available in the data
        all_keys = set()
        comparison_key_values = {key: set() for key in comparison_keys}

        for record in records:
            # Use distinguishing labels directly for analysis
            labels = record.distinguishing_labels

            # Debug: Print version values found
            if "version" in labels:
                logger.debug(
                    f"🐛 DEBUG: Found version='{labels['version']}' in record {record.test_base_path}"
                )
            else:
                logger.debug(
                    f"🐛 DEBUG: No 'version' key found in record {record.test_base_path}, keys: {list(labels.keys())}"
                )

            all_keys.update(labels.keys())
            for key in comparison_keys:
                if key in labels:
                    comparison_key_values[key].add(labels[key])

        logger.info("\n📊 Data analysis for comparison report:")
        logger.info(f"   Total records: {len(records)}")
        logger.info(f"   Available label keys: {sorted(all_keys)}")
        logger.info("   Comparison key availability:")
        for key in comparison_keys:
            values = comparison_key_values[key]
            logger.info(
                f"      {key}: {len(values)} unique values: {sorted(values) if values else 'NOT FOUND'}"
            )

        # If version key has multiple values, use it; otherwise try deployment_profile fallback
        active_comparison_keys = []
        for key in comparison_keys:
            if len(comparison_key_values[key]) > 1:
                active_comparison_keys.append(key)

        if not active_comparison_keys:
            logger.info("⚠️  No version key with multiple values found, trying fallback keys...")
            fallback_keys = ["deployment_profile", "guidellm_loadshape"]
            fallback_values = {}

            for record in records:
                # Use distinguishing labels directly for fallback logic
                labels = record.distinguishing_labels

                for key in fallback_keys:
                    if key not in fallback_values:
                        fallback_values[key] = set()

                    # Use distinguishing labels directly
                    value = labels.get(key, "default")

                    fallback_values[key].add(value)

            logger.info("   Fallback key availability:")
            for key in fallback_keys:
                values = fallback_values[key]
                logger.info(f"      {key}: {len(values)} unique values: {sorted(values)}")
                if len(values) > 1:
                    active_comparison_keys.append(key)

        if not active_comparison_keys:
            logger.info("⚠️  No suitable version values found for comparison")
            logger.info("   📊 Generating single-dataset report...")

            # Use a placeholder comparison key set for single-group analysis
            active_comparison_keys = ["dataset"]
            comparison_keys = {"dataset"}

        logger.info(f"   Using comparison keys: {active_comparison_keys}")
        comparison_keys = set(active_comparison_keys)

        # Create one unified group for version comparison across all test configurations
        core_labels = all_keys

        comparison_groups = {}

        for record in records:
            # Create a group key from only core distinguishing labels (not version or config details)
            group_labels = {}
            for k, v in record.distinguishing_labels.items():
                if k not in comparison_keys and k in core_labels:
                    group_labels[k] = v

            # Convert to a sortable tuple for grouping - if no core labels, use a single group
            if group_labels:
                group_key = tuple(sorted(group_labels.items()))
            else:
                group_key = ("unified_comparison",)

            if group_key not in comparison_groups:
                comparison_groups[group_key] = []
            comparison_groups[group_key].append(record)

        # Filter to only groups that have records with different comparison key values
        valid_groups = {}
        for group_key, group_records in comparison_groups.items():
            # Extract comparison key values for this group
            comparison_values = set()
            for record in group_records:
                # Use distinguishing labels directly for comparison key evaluation
                comp_values = []
                for key in comparison_keys:
                    value = record.distinguishing_labels.get(key, "unknown")
                    comp_values.append(value)
                comparison_values.add(tuple(comp_values))

            # Only include groups with multiple comparison key values
            if len(comparison_values) > 1:
                valid_groups[group_key] = group_records

        if not valid_groups:
            logger.info(
                f"⚠️  No comparison groups found - all records have identical values for keys: {active_comparison_keys}"
            )
            logger.info("   📊 Generating single-group report with all data...")

            # Create a single group with all records and include warning in the description
            warning_message = f"No comparisons possible - all records have identical {', '.join(active_comparison_keys)} values"
            single_group_key = ("all_data", warning_message)
            valid_groups = {single_group_key: records}

        logger.info(
            f"\n🔍 Found {len(valid_groups)} comparison groups with varying {active_comparison_keys}:"
        )
        for i, (group_key, group_records) in enumerate(valid_groups.items(), 1):
            # Handle special case for warning message
            if len(group_key) == 2 and group_key[0] == "all_data":
                group_desc = "All Available Data"
            else:
                group_desc = ", ".join(f"{k}={v}" for k, v in group_key)
            comp_values = set()
            for record in group_records:
                # Use distinguishing labels directly for comparison key evaluation
                comp_vals = []
                for key in comparison_keys:
                    value = record.distinguishing_labels.get(key, "unknown")
                    comp_vals.append(value)
                comp_values.add(tuple(comp_vals))
            comp_desc = " vs ".join(f"{':'.join(cv)}" for cv in sorted(comp_values))
            logger.info(f"   📋 Group {i}: [{group_desc}] comparing [{comp_desc}]")

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
            report_dir_name = f"report_{report_number:02d}_comparison_analysis"
        else:
            report_dir_name = "comparison_analysis"

        report_dir = output_dir / report_dir_name
        report_dir.mkdir(exist_ok=True)
        logger.info(f"\n📁 Created report directory: {report_dir_name}")

        all_plots_data = []

        # Process each comparison group separately
        for group_key, group_records in valid_groups.items():
            # Create group description for display and directory naming
            # Handle special case for warning message
            if len(group_key) == 2 and group_key[0] == "all_data":
                group_desc = "All Available Data"
                group_name = "all_data"
                warning_msg = group_key[1]  # Extract warning message
            else:
                group_desc = ", ".join(f"{k}={v}" for k, v in group_key)
                group_name = "__".join(f"{k}_{sanitize_for_path(v)}" for k, v in group_key)
                warning_msg = None

            logger.info(f"\n📊 Processing comparison group: {group_desc}")
            logger.info(f"   Records: {len(group_records)}")

            # Create DataFrame for this comparison group
            df = create_dataframe_from_records(group_records)
            if df.empty:
                logger.info(f"   ⚠️  No data available for group: {group_desc}")
                continue

            logger.info(f"   📈 Generating {len(plot_functions)} plots for {group_desc}...")

            # Create subdirectory for this comparison group
            group_dir = report_dir / group_name
            group_dir.mkdir(exist_ok=True)

            # Extract metadata for this group
            comp_values = set()
            for r in group_records:
                comp_tuple = tuple(
                    r.distinguishing_labels.get(key, "unknown") for key in comparison_keys
                )
                comp_values.add(comp_tuple)
            comp_desc = " vs ".join(f"{':'.join(cv)}" for cv in sorted(comp_values))

            group_plots = []
            for i, (plot_name, plot_func) in enumerate(plot_functions, 1):
                logger.info(
                    f"   📊 [{i}/{len(plot_functions)}] Creating {plot_name} for comparison group..."
                )
                try:
                    # Create subtitle with group description and comparison info
                    if warning_msg:
                        # Show warning message instead of comparison info
                        subtitle = f"{group_desc} - ⚠️ {warning_msg}"
                    else:
                        subtitle = f"Comparison Group: {group_desc} | Comparing: {comp_desc}"

                    # Add original title context if provided
                    if title_context:
                        group_title_context = f"{title_context}<br><sub>{subtitle}</sub>"
                    else:
                        group_title_context = f"<br><sub>{subtitle}</sub>"

                    fig = plot_func(df, group_title_context)
                    if fig:
                        # Save as both PNG and HTML in the group directory
                        filename = f"{group_name}_{plot_name.lower().replace(' ', '_')}"

                        # Save PNG image
                        config = PLOT_CONFIG_LARGE if "Percentiles" in plot_name else PLOT_CONFIG
                        width = config["width"]
                        height = config["height"]
                        png_path = save_figure(
                            fig, group_dir, filename, as_image=True, width=width, height=height
                        )

                        # Save HTML version
                        html_path = save_figure(fig, group_dir, filename, as_image=False)

                        if html_path:
                            # Store relative paths for linking (PNG optional)
                            png_rel_path = (
                                f"{report_dir_name}/{group_name}/{Path(png_path).name}"
                                if png_path
                                else None
                            )
                            group_plots.append(
                                (
                                    plot_name,
                                    png_rel_path,  # PNG path (can be None)
                                    f"{report_dir_name}/{group_name}/{Path(html_path).name}",  # HTML path
                                )
                            )
                            logger.info(f"   ✅ {plot_name} saved for comparison group")
                            if not png_path:
                                logger.warning(
                                    f"   ⚠️  PNG version of {plot_name} could not be generated (HTML available)"
                                )
                    else:
                        logger.info(
                            f"   ⚠️  {plot_name} could not be created for comparison group (no figure returned)"
                        )

                except Exception as e:
                    logger.info(f"   ❌ Failed to generate {plot_name} for comparison group: {e}")

            # Store group plots data if any plots were created
            if group_plots:
                # Include group_key for displaying identical labels
                group_labels_dict = dict(group_key) if group_key != ("unified_comparison",) else {}
                all_plots_data.append((group_desc, group_plots, group_labels_dict))

        if not all_plots_data:
            logger.info("❌ No plots were successfully generated")
            return None

        logger.info(
            f"\n✅ Successfully generated plots for {len(all_plots_data)} comparison group(s)!"
        )

        # Create comprehensive HTML report with sections for each comparison group
        logger.info("\n📝 Assembling comprehensive HTML report...")
        logger.info(
            "   🔗 Creating report with comparison group sections and interactive HTML links..."
        )

        html_content = f"""
<!DOCTYPE html>
<html lang='en'>
<head>
    <meta charset='UTF-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1.0'>
    <title>{display_title}</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        .profile-section {{ margin: 40px 0; }}
        .profile-title {{ color: #333; font-size: 24px; margin-bottom: 20px; border-bottom: 2px solid #007acc; padding-bottom: 10px; }}
        .navigation {{ background-color: #f5f5f5; padding: 15px; border-radius: 4px; margin-bottom: 20px; }}
        .navigation ul {{ list-style: none; padding: 0; margin: 0; }}
        .navigation li {{ display: inline-block; margin-right: 20px; }}
        .navigation a {{ color: #007acc; text-decoration: none; font-weight: bold; }}
        .navigation a:hover {{ text-decoration: underline; }}

        .tabs-container {{
            margin: 20px 0;
            border: 1px solid #ddd;
            border-radius: 8px;
            background-color: #f9f9f9;
        }}

        .tab-headers {{
            display: flex;
            background-color: #f1f1f1;
            border-bottom: 1px solid #ddd;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            flex-wrap: wrap;
        }}

        .tab-header {{
            background-color: #e1e1e1;
            color: #333;
            padding: 15px 20px;
            cursor: pointer;
            border: none;
            border-right: 1px solid #ddd;
            font-family: Arial, sans-serif;
            font-size: 16px;
            font-weight: bold;
            transition: background-color 0.3s;
            flex: 1;
            min-width: 150px;
        }}

        .tab-header:hover {{
            background-color: #d1d1d1;
        }}

        .tab-header.active {{
            background-color: #4CAF50;
            color: white;
        }}

        .tab-header:first-child {{
            border-top-left-radius: 8px;
        }}

        .tab-header:last-child {{
            border-right: none;
            border-top-right-radius: 8px;
        }}

        .tab-content {{
            padding: 20px;
            background-color: white;
            display: none;
            border-bottom-left-radius: 8px;
            border-bottom-right-radius: 8px;
            width: 100%;
            min-height: 600px;
        }}

        .tab-content.active {{
            display: block;
            height: auto !important;
            min-height: auto !important;
            width: 100%;
        }}

        .tab-content > div {{
            width: 100% !important;
            height: auto !important;
        }}

        .tab-content .plotly-graph-div {{
            width: 100% !important;
            height: 600px !important;
            min-width: 100% !important;
            max-width: 100% !important;
        }}

        .tab-content .plotly-graph-div .svg-container {{
            width: 100% !important;
            height: 100% !important;
        }}

        .tab-content .js-plotly-plot {{
            width: 100% !important;
            height: 600px !important;
        }}

        details {{
            height: auto !important;
            min-height: auto !important;
        }}

        details[open] {{
            height: auto !important;
            min-height: auto !important;
        }}

        details > div {{
            height: auto !important;
            min-height: auto !important;
        }}

        .performance-insights {{
            border-radius: 5px;
            padding: 0.5em;
            background-color: lightgray;
            margin-top: 15px;
        }}

        .plot-container {{
            text-align: center;
        }}

        .plot-container img {{
            max-width: 100%;
            height: auto;
            cursor: pointer;
            border: 1px solid #ddd;
            border-radius: 4px;
        }}

    </style>

    <script>
    function showTab(containerId, tabId, buttonElement) {{
        // Get the specific container
        var container = document.getElementById(containerId);

        // Hide all tab contents within this container
        var contents = container.querySelectorAll('.tab-content');
        for (var i = 0; i < contents.length; i++) {{
            contents[i].classList.remove('active');
        }}

        // Remove active class from all headers within this container
        var headers = container.querySelectorAll('.tab-header');
        for (var i = 0; i < headers.length; i++) {{
            headers[i].classList.remove('active');
        }}

        // Show selected tab content and mark header as active
        document.getElementById(tabId).classList.add('active');
        buttonElement.classList.add('active');

        // Resize any Plotly plots in the newly visible tab
        setTimeout(function() {{
            var activeTab = document.getElementById(tabId);
            if (activeTab && typeof Plotly !== 'undefined') {{
                var plotlyDivs = activeTab.querySelectorAll('.plotly-graph-div');
                for (var i = 0; i < plotlyDivs.length; i++) {{
                    var plotDiv = plotlyDivs[i];

                    // Force the plot div to use full container width
                    plotDiv.style.width = '100%';
                    plotDiv.style.height = '600px';

                    // Get the actual container width
                    var containerWidth = plotDiv.parentNode.clientWidth;

                    // Force relayout with explicit dimensions
                    if (plotDiv._fullLayout) {{
                        Plotly.relayout(plotDiv, {{
                            width: containerWidth,
                            height: 600,
                            autosize: true
                        }});
                    }} else {{
                        // Fallback to resize if relayout not available
                        Plotly.Plots.resize(plotDiv);
                    }}
                }}
            }}
        }}, 150);
    }}
    </script>
</head>
<body>
    <p><i>Click on the image to open the interactive full-size view of the plot.</i><br/>
    <i>In the interactive view, click in the legend to hide a line, double click to see only this line.</i></p>

    <div class="header">
        <h1>{display_title}</h1>
        <p>Generated performance analysis with separate visualizations for each comparison group</p>
    </div>

    <div class="navigation">
        <h3>Quick Navigation:</h3>
        <ul>"""

        # Add navigation links for each comparison group
        for group_desc, _, _ in all_plots_data:
            group_id = group_desc.replace(" ", "_").replace("=", "_").replace(",", "_")
            html_content += f'\n            <li><a href="#{group_id}">{group_desc}</a></li>'

        html_content += """
        </ul>
    </div>
"""
        # Add sections for each comparison group
        for group_desc, plots, group_labels in all_plots_data:
            group_id = group_desc.replace(" ", "_").replace("=", "_").replace(",", "_")

            # Handle special warning case
            if group_desc == "All Available Data":
                title_prefix = "Dataset:"
                explanation = (
                    "⚠️ No comparisons possible - all records have identical version values"
                )
            else:
                title_prefix = "Comparison Group:"
                explanation = f"Comparing different values of {active_comparison_keys} across identical test conditions"

            # Generate display text for all identical labels
            if group_labels:
                labels_html = (
                    "<ul style='margin: 5px 0; padding-left: 20px;'>"
                    + "".join(
                        f"<li><strong>{k}</strong>: {v}</li>"
                        for k, v in sorted(group_labels.items())
                    )
                    + "</ul>"
                )
            else:
                labels_html = "<p>All data (no grouping constraints)</p>"

            html_content += f"""
    <div class="profile-section" id="{group_id}">
        <h2 class="profile-title">{title_prefix} {group_desc}</h2>
        <p style="color: #666; font-size: 16px; margin: -10px 0 20px 0; font-style: italic;">
            {explanation}
        </p>

        <div style="background-color: #f8f9fa; padding: 12px; margin: 10px 0 20px 0; border-left: 4px solid #007acc; font-size: 14px; color: #495057;">
            <strong>Identical labels:</strong>
            {labels_html}
        </div>

        """

            # Embed all plots for this comparison group
            if plots:
                # Define plot descriptions
                descriptions = {
                    "Token Throughput vs Concurrency": "Token generation throughput scaling analysis across different concurrency levels.",
                    "TTFT Analysis": "Time To First Token analysis - measuring responsiveness and initial latency.",
                    "Token Throughput Percentiles": "Complete token throughput percentile distribution analysis.",
                    "Throughput Scaling": "Throughput scaling behavior and efficiency analysis.",
                    "Latency vs Throughput": "Trade-off analysis between latency and throughput performance.",
                }

                for plot_name, png_path, html_path in plots:
                    description = descriptions.get(plot_name, f"{plot_name} performance analysis.")
                    # Use centralized embedding function
                    plot_content = embed_plot_for_report(png_path, html_path, plot_name, output_dir)
                    html_content += f"""
        <div style='padding:20px; margin-bottom:30px; height: auto; min-height: auto;'>
            <h4>📊 {plot_name}</h4>
            <p>{description}</p>
            {plot_content}
        </div>"""

                html_content += "</div>"
            else:
                html_content += """
        <div style='padding:20px;'>
            <p>⚠️ No plots available for this group.</p>
        </div>
    </div>"""

        # Close the HTML after all groups are processed
        html_content += """
</body>
</html>"""

        # Save the main HTML report
        main_html_filename = create_report_filename(
            "comparison_analysis", report_number, report_title, "html"
        )
        main_html_path = output_dir / main_html_filename

        with open(main_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        logger.info(f"✅ Comparison analysis report saved as: {main_html_filename}")
        logger.info(f"📁 Individual plots organized in subdirectories under: {report_dir_name}/")

        logger.info("=" * 70)
        logger.info(f"🎉 {display_title} ready: {main_html_path.name}")

        return str(main_html_path)

    except Exception:
        logger.exception("❌ Failed to generate deployment profile report")
        # Re-raise for debugging - this error should not be silently ignored
        raise


def generate_comprehensive_performance_report(
    records: list[UnifiedResultRecord],
    output_dir: Path,
    title_context: str = "",
    report_number: int | None = None,
    report_title: str = "GuideLLM Performance Analysis",
) -> str | None:
    """
    Generate comprehensive performance analysis report with separate plots for each loadshape.

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

        logger.info(f"\n🚀 Starting {display_title} generation...")
        logger.info("=" * 70)

        # Group records by guidellm_loadshape
        loadshape_groups = {}
        for record in records:
            loadshape = record.distinguishing_labels.get("guidellm_loadshape", "default")
            if loadshape not in loadshape_groups:
                loadshape_groups[loadshape] = []
            loadshape_groups[loadshape].append(record)

        if not loadshape_groups:
            logger.info("❌ No data available for analysis")
            return None

        logger.info(
            f"\n🔍 Found {len(loadshape_groups)} loadshape(s): {', '.join(loadshape_groups.keys())}"
        )

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
        logger.info(f"\n📁 Created report directory: {report_dir_name}")

        all_plots_data = []

        # Process each loadshape separately
        for loadshape, loadshape_records in loadshape_groups.items():
            logger.info(f"\n📊 Processing loadshape: {loadshape}")
            logger.info(f"   Records: {len(loadshape_records)}")

            # Create DataFrame for this loadshape
            df = create_dataframe_from_records(loadshape_records)
            if df.empty:
                logger.info(f"   ⚠️  No data available for loadshape: {loadshape}")
                continue

            logger.info(f"   📈 Generating {len(plot_functions)} plots for {loadshape}...")

            # Create subdirectory for this loadshape
            loadshape_dir = report_dir / sanitize_for_path(loadshape)
            loadshape_dir.mkdir(exist_ok=True)

            # Extract model information for this loadshape
            model_info = "Unknown"
            if loadshape_records:
                # Try to get model information from distinguishing labels
                first_record = loadshape_records[0]
                model_info = (
                    first_record.distinguishing_labels.get("model")
                    or first_record.distinguishing_labels.get("model_name")
                    or first_record.distinguishing_labels.get("llm_model")
                    or first_record.run_identity.get("model")
                    or "Unknown"
                )

            loadshape_plots = []
            for i, (plot_name, plot_func) in enumerate(plot_functions, 1):
                logger.info(
                    f"   📊 [{i}/{len(plot_functions)}] Creating {plot_name} for {loadshape}..."
                )
                try:
                    # Create subtitle with model and loadshape info
                    subtitle = f"Model: {model_info} | Load Shape: {loadshape}"

                    # Add original title context if provided
                    if title_context:
                        loadshape_title_context = f"{title_context}<br><sub>{subtitle}</sub>"
                    else:
                        loadshape_title_context = f"<br><sub>{subtitle}</sub>"

                    fig = plot_func(df, loadshape_title_context)
                    if fig:
                        # Save as both PNG and HTML in the loadshape directory
                        filename = f"{loadshape}_{plot_name.lower().replace(' ', '_')}"

                        # Save PNG image
                        config = PLOT_CONFIG_LARGE if "Percentiles" in plot_name else PLOT_CONFIG
                        width = config["width"]
                        height = config["height"]
                        png_path = save_figure(
                            fig, loadshape_dir, filename, as_image=True, width=width, height=height
                        )

                        # Save HTML version
                        html_path = save_figure(fig, loadshape_dir, filename, as_image=False)

                        if html_path:
                            # Store relative paths for linking (PNG optional)
                            png_rel_path = (
                                f"{report_dir_name}/{loadshape}/{Path(png_path).name}"
                                if png_path
                                else None
                            )
                            html_rel_path = f"{report_dir_name}/{loadshape}/{Path(html_path).name}"
                            loadshape_plots.append(
                                (
                                    plot_name,
                                    png_rel_path,  # PNG path (can be None)
                                    html_rel_path,  # HTML path
                                )
                            )
                            logger.info(
                                f"   ✅ {plot_name} saved - PNG: {png_rel_path}, HTML: {html_rel_path}"
                            )
                            if not png_path:
                                logger.warning(
                                    f"   ⚠️  PNG version of {plot_name} could not be generated (HTML available)"
                                )
                        else:
                            logger.warning(f"   ⚠️  HTML version of {plot_name} could not be saved")
                    else:
                        logger.warning(
                            f"   ⚠️  {plot_name} could not be created for {loadshape} (no figure returned)"
                        )

                except Exception as e:
                    logger.info(f"   ❌ Failed to generate {plot_name} for {loadshape}: {e}")

            # Store loadshape plots data
            if loadshape_plots:
                all_plots_data.append((loadshape, loadshape_plots))

        if not all_plots_data:
            logger.info("❌ No plots were successfully generated")
            return None

        logger.info(f"\n✅ Successfully generated plots for {len(all_plots_data)} loadshape(s)!")

        # Create comprehensive HTML report with sections for each loadshape
        logger.info("\n📝 Assembling comprehensive HTML report...")
        logger.info("   🔗 Creating report with loadshape sections and interactive HTML links...")

        html_content = f"""
<!DOCTYPE html>
<html lang='en'>
<head>
    <meta charset='UTF-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1.0'>
    <title>{display_title}</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body {{font-family: Arial, sans-serif; margin: 40px; }}
        .header {{text-align: center; margin-bottom: 30px; }}
        .loadshape-section {{margin: 40px 0; }}
        .loadshape-title {{color: #333; font-size: 24px; margin-bottom: 20px; border-bottom: 2px solid #007acc; padding-bottom: 10px; }}
        .navigation {{background-color: #f5f5f5; padding: 15px; border-radius: 4px; margin-bottom: 20px; }}
        .navigation ul {{list-style: none; padding: 0; margin: 0; }}
        .navigation li {{display: inline-block; margin-right: 20px; }}
        .navigation a {{color: #007acc; text-decoration: none; font-weight: bold; }}
        .navigation a:hover {{text-decoration: underline; }}

        .tabs-container {{
            margin: 20px 0;
            border: 1px solid #ddd;
            border-radius: 8px;
            background-color: #f9f9f9;
        }}

        .tab-headers {{
            display: flex;
            background-color: #f1f1f1;
            border-bottom: 1px solid #ddd;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            flex-wrap: wrap;
        }}

        .tab-header {{
            background-color: #e1e1e1;
            color: #333;
            padding: 15px 20px;
            cursor: pointer;
            border: none;
            border-right: 1px solid #ddd;
            font-family: Arial, sans-serif;
            font-size: 16px;
            font-weight: bold;
            transition: background-color 0.3s;
            flex: 1;
            min-width: 150px;
        }}

        .tab-header:hover {{
            background-color: #d1d1d1;
        }}

        .tab-header.active {{
            background-color: #4CAF50;
            color: white;
        }}

        .tab-header:first-child {{
            border-top-left-radius: 8px;
        }}

        .tab-header:last-child {{
            border-right: none;
            border-top-right-radius: 8px;
        }}

        .tab-content {{
            padding: 20px;
            background-color: white;
            display: none;
            border-bottom-left-radius: 8px;
            border-bottom-right-radius: 8px;
            width: 100%;
            min-height: 600px;
        }}

        .tab-content.active {{
            display: block;
            height: auto !important;
            min-height: auto !important;
            width: 100%;
        }}

        .tab-content > div {{
            width: 100% !important;
            height: auto !important;
        }}

        .tab-content .plotly-graph-div {{
            width: 100% !important;
            height: 600px !important;
            min-width: 100% !important;
            max-width: 100% !important;
        }}

        .tab-content .plotly-graph-div .svg-container {{
            width: 100% !important;
            height: 100% !important;
        }}

        .tab-content .js-plotly-plot {{
            width: 100% !important;
            height: 600px !important;
        }}

        details {{
            height: auto !important;
            min-height: auto !important;
        }}

        details[open] {{
            height: auto !important;
            min-height: auto !important;
        }}

        details > div {{
            height: auto !important;
            min-height: auto !important;
        }}

        .performance-insights {{
            border-radius: 5px;
            padding: 0.5em;
            background-color: lightgray;
            margin-top: 15px;
        }}

        .plot-container {{
            text-align: center;
        }}

        .plot-container img {{
            max-width: 100%;
            height: auto;
            cursor: pointer;
            border: 1px solid #ddd;
            border-radius: 4px;
        }}

    </style>

    <script>
    function showTab(containerId, tabId, buttonElement) {{
        // Get the specific container
        var container = document.getElementById(containerId);

        // Hide all tab contents within this container
        var contents = container.querySelectorAll('.tab-content');
        for (var i = 0; i < contents.length; i++) {{
            contents[i].classList.remove('active');
        }}

        // Remove active class from all headers within this container
        var headers = container.querySelectorAll('.tab-header');
        for (var i = 0; i < headers.length; i++) {{
            headers[i].classList.remove('active');
        }}

        // Show selected tab content and mark header as active
        document.getElementById(tabId).classList.add('active');
        buttonElement.classList.add('active');

        // Resize any Plotly plots in the newly visible tab
        setTimeout(function() {{
            var activeTab = document.getElementById(tabId);
            if (activeTab && typeof Plotly !== 'undefined') {{
                var plotlyDivs = activeTab.querySelectorAll('.plotly-graph-div');
                for (var i = 0; i < plotlyDivs.length; i++) {{
                    var plotDiv = plotlyDivs[i];

                    // Force the plot div to use full container width
                    plotDiv.style.width = '100%';
                    plotDiv.style.height = '600px';

                    // Get the actual container width
                    var containerWidth = plotDiv.parentNode.clientWidth;

                    // Force relayout with explicit dimensions
                    if (plotDiv._fullLayout) {{
                        Plotly.relayout(plotDiv, {{
                            width: containerWidth,
                            height: 600,
                            autosize: true
                        }});
                    }} else {{
                        // Fallback to resize if relayout not available
                        Plotly.Plots.resize(plotDiv);
                    }}
                }}
            }}
        }}, 150);
    }}
    </script>
</head>
<body>
    <p><i>Click on the image to open the interactive full-size view of the plot.</i><br/>
    <i>In the interactive view, click in the legend to hide a line, double click to see only this line.</i></p>

    <div class="header">
        <h1>{display_title}</h1>
        <p>Generated performance analysis with separate visualizations for each loadshape</p>
    </div>

    <div class="navigation">
        <h3>Quick Navigation:</h3>
        <ul>"""

        # Add navigation links for each loadshape
        for loadshape, _ in all_plots_data:
            html_content += f'\n            <li><a href="#{loadshape}">{loadshape}</a></li>'

        html_content += """
        </ul>
    </div>
"""

        # Add sections for each loadshape with tabbed interface
        container_id = 0
        for loadshape, plots in all_plots_data:
            # Extract model information for this loadshape
            loadshape_records = loadshape_groups[loadshape]
            model_info = "Unknown"
            if loadshape_records:
                # Try to get model information from distinguishing labels
                first_record = loadshape_records[0]
                model_info = (
                    first_record.distinguishing_labels.get("model")
                    or first_record.distinguishing_labels.get("model_name")
                    or first_record.distinguishing_labels.get("llm_model")
                    or first_record.run_identity.get("model")
                    or "Unknown"
                )

            html_content += f"""
    <div class="loadshape-section" id="{loadshape}">
        <h2 class="loadshape-title">Loadshape: {loadshape}</h2>
        <p style="color: #666; font-size: 16px; margin: -10px 0 20px 0; font-style: italic;">
            Model: {model_info} | Load Shape: {loadshape}
        </p>

        <div id='tabs-container-{container_id}' class='tabs-container'>
            <div class='tab-headers'>"""

            # Define tab mapping with icons
            tab_mapping = {
                "Token Throughput vs Concurrency": "🚀 Throughput",
                "TTFT Analysis": "⏱️ TTFT",
                "Token Throughput Percentiles": "📊 Throughput Percentiles",
                "Throughput Scaling": "📈 Scaling",
                "Latency vs Throughput": "⚖️ Latency Trade-off",
            }

            # Create tab headers
            for tab_idx, (plot_name, _png_path, _html_path) in enumerate(plots):
                tab_title = tab_mapping.get(plot_name, plot_name)
                active_class = " active" if tab_idx == 0 else ""
                html_content += f"""
                <button class='tab-header{active_class}' onclick="showTab('tabs-container-{container_id}', 'tab-{container_id}-{tab_idx}', this)">{tab_title}</button>"""

            html_content += """
            </div>"""

            # Create tab contents
            descriptions = {
                "Token Throughput vs Concurrency": "Token generation throughput scaling analysis across different concurrency levels.",
                "TTFT Analysis": "Time To First Token analysis - measuring responsiveness and initial latency.",
                "Token Throughput Percentiles": "Complete token throughput percentile distribution analysis.",
                "Throughput Scaling": "Throughput scaling behavior and efficiency analysis.",
                "Latency vs Throughput": "Trade-off analysis between latency and throughput performance.",
            }

            for tab_idx, (plot_name, png_path, html_path) in enumerate(plots):
                active_class = " active" if tab_idx == 0 else ""
                description = descriptions.get(plot_name, f"{plot_name} performance analysis.")
                # Use centralized embedding function
                plot_content = embed_plot_for_report(png_path, html_path, plot_name, output_dir)

                html_content += f"""
            <div id='tab-{container_id}-{tab_idx}' class='tab-content{active_class}' style='height: auto; min-height: auto;'>
                <div style='padding:20px; height: auto; min-height: auto;'>
                    <h4>{plot_name}</h4>
                    <p>{description}</p>
                    {plot_content}
                </div>
            </div>"""

            html_content += """
        </div>
    </div>"""
            container_id += 1

        html_content += """
</body>
</html>"""

        # Save the main HTML report
        main_html_filename = create_report_filename(
            report_title.lower().replace(" ", "_"), report_number, report_title, "html"
        )
        main_html_path = output_dir / main_html_filename

        with open(main_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        logger.info(f"✅ Comprehensive report saved as: {main_html_filename}")
        logger.info(f"📁 Individual plots organized in subdirectories under: {report_dir_name}/")

        logger.info("=" * 70)
        logger.info(f"🎉 {display_title} ready: {main_html_path.name}")

        return str(main_html_path)

    except Exception:
        logger.exception("❌ Failed to generate comprehensive performance report")
        # Re-raise for debugging - this error should not be silently ignored
        raise


def _generate_performance_summary(df: pd.DataFrame) -> dict[str, Any]:
    """Generate performance summary statistics from the dataframe."""
    if df.empty:
        logger.info("⚠️  No data available for performance summary")
        return {}

    logger.info("   🔍 Analyzing best performers across all metrics...")

    # Find best performers
    best_tokens_idx = df["tokens_per_second"].idxmax()
    best_efficiency_idx = (
        df["tokens_per_second"] / df["intended_concurrency"].replace(0, 1)
    ).idxmax()
    best_ttft_idx = df["ttft_median_ms"].idxmin()

    # Configuration analysis
    logger.info("   📊 Computing configuration performance rankings...")
    config_performance = (
        df.groupby("test_configuration")
        .agg(
            {
                "tokens_per_second": "max",
                "ttft_median_ms": "mean",
                "request_rate": "max",
                "intended_concurrency": "max",
                "request_concurrency": "max",
            }
        )
        .sort_values("tokens_per_second", ascending=False)
    )

    logger.info(f"   ✅ Performance analysis complete for {len(config_performance)} configurations")

    return {
        "total_configurations": len(df["test_configuration"].unique()),
        "total_strategies": len(df),
        "best_tokens": {
            "value": df.loc[best_tokens_idx, "tokens_per_second"],
            "config": df.loc[best_tokens_idx, "test_configuration"],
            "strategy": df.loc[best_tokens_idx, "strategy"],
            "concurrency": df.loc[best_tokens_idx, "intended_concurrency"],
        },
        "best_efficiency": {
            "value": df.loc[best_efficiency_idx, "tokens_per_second"]
            / max(df.loc[best_efficiency_idx, "intended_concurrency"], 1),
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
    output_dir: Path,
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
            # Use centralized embedding function
            plot_content = embed_plot_for_report(png_path, html_path, plot_name, output_dir)
            html_parts.append(f"""
    <div class="plot-section">
        <h3>📈 {plot_name}</h3>
        {plot_content}
        <br>
        <small>💡 Performance visualization with comprehensive metrics analysis</small>
    </div>""")

    # Footer
    html_parts.append("""
</body>
</html>""")

    return "".join(html_parts)
