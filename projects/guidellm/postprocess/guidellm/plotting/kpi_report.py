"""KPI report generation for GuideLLM benchmarks."""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi import (
    KpiRecord,
)
from projects.caliper.postprocess.helpers.visualization_utils import (
    create_report_filename,
)

logger = logging.getLogger(__name__)


def generate_kpi_report(
    records: list[Any],
    output_dir: Path,
    report_title: str = "GuideLLM KPI Report",
    report_number: int | None = None,
) -> str:
    """
    Generate an HTML KPI report showing test conditions and KPI values.

    Args:
        records: List of unified result records
        output_dir: Directory to write the HTML report
        report_title: Title for the report

    Returns:
        Path to the generated HTML file
    """
    from projects.llm_d.postprocess.llm_d.parsing.kpis import GuideLLMKpiHandler

    if not records:
        return ""

    # Handle multiple records by creating sections for each test
    if len(records) > 1:
        logger.info(f"📊 Generating multi-test KPI report for {len(records)} test records")
        return _generate_multi_test_kpi_report(records, output_dir, report_title, report_number)

    # Use the single record to extract test conditions and KPIs
    first_record = records[0]

    # Extract test condition labels
    test_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(first_record)

    # Get distinguishing labels too
    base_labels = {**first_record.distinguishing_labels}

    # Merge all labels
    all_labels = {**base_labels, **test_labels}

    # Compute KPIs for the first record (representative of the test)
    # We can compute KPIs directly from the records without creating a full model

    from projects.caliper.engine.kpi import get_kpi_functions, is_curve_kpi

    # Get the KPI functions from the GuideLLM module
    from projects.guidellm.postprocess.guidellm.parsing import kpis as kpis_module

    kpi_functions = get_kpi_functions(kpis_module)

    # Compute each KPI value
    kpi_data = []

    for kpi_id, kpi_func in kpi_functions.items():
        try:
            value = kpi_func(first_record)
        except (TypeError, ValueError, KeyError):
            if is_curve_kpi(kpi_func):
                value = []  # Empty list for failed curve KPIs
            else:
                value = None  # None for missing/failed scalar KPIs

        # Create structured KPI record using core dataclass
        kpi_record = KpiRecord(
            schema_version="1",
            kpi_id=kpi_id,
            value=value if value is not None else 0,  # KpiRecord requires non-null value
            unit=kpi_func._kpi_unit,
            run_id=first_record.test_base_path,
            timestamp="",  # Not critical for display
            labels={"higher_is_better": kpi_func._kpi_higher_is_better},
            metadata={
                "help": kpi_func._kpi_help,
                "format": getattr(kpi_func, "_kpi_format", "{:.2f}"),
                "is_curve": is_curve_kpi(kpi_func),
            },
        )

        # Add 2D-specific metadata if applicable
        if is_curve_kpi(kpi_func):
            kpi_record.x_unit = kpi_func._kpi_x_unit
            kpi_record.x_help = kpi_func._kpi_x_help
            kpi_record.y_unit = getattr(kpi_func, "_kpi_y_unit", None) or kpi_func._kpi_unit
            kpi_record.y_help = getattr(kpi_func, "_kpi_y_help", None) or kpi_func._kpi_help
            # Add formatting info to metadata
            kpi_record.metadata["x_format"] = getattr(kpi_func, "_kpi_x_format", "{:.1f}")
            kpi_record.metadata["y_format"] = getattr(kpi_func, "_kpi_y_format", "{:.1f}")

        # Skip null values for display (can't create KpiRecord with None value)
        if value is not None:
            kpi_data.append(kpi_record.to_dict())

    # Extract metadata fields
    metadata = GuideLLMKpiHandler.extract_metadata(first_record)

    # Generate HTML
    html_content = _generate_html(
        report_title=report_title,
        test_labels=all_labels,
        kpi_data=kpi_data,
        test_info=_extract_test_info(first_record),
        metadata=metadata,
    )

    # Write to file
    filename = create_report_filename("kpi_summary", report_number, report_title, "html")
    output_file = output_dir / filename
    output_file.write_text(html_content, encoding="utf-8")

    return str(output_file)


def _extract_test_info(record: Any) -> dict[str, Any]:
    """Extract basic test information for display."""
    return {
        "test_path": str(record.test_base_path),
        "run_name": record.metrics.get("run_name", "unknown"),
        "timestamp": record.metrics.get("timestamp", "unknown"),
        "duration": f"{record.metrics.get('test_duration_s', 0):.1f}s",
        "total_requests": record.metrics.get("total_requests", 0),
        "successful_requests": record.metrics.get("successful_requests", 0),
    }


def _generate_html(
    report_title: str,
    test_labels: dict[str, Any],
    kpi_data: list[dict[str, Any]],
    test_info: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    """Generate the HTML content for the KPI report."""

    # Sort KPIs by category and name
    scalar_kpis = [kpi for kpi in kpi_data if not kpi.get("is_curve", False)]
    curve_kpis = [kpi for kpi in kpi_data if kpi.get("is_curve", False)]

    # Sort within each category
    scalar_kpis.sort(key=lambda k: k["kpi_id"])
    curve_kpis.sort(key=lambda k: k["kpi_id"])

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(report_title)}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
            margin-bottom: 30px;
        }}
        h2 {{
            color: #34495e;
            margin-top: 30px;
            margin-bottom: 15px;
            border-left: 4px solid #3498db;
            padding-left: 10px;
        }}
        h3 {{
            color: #7f8c8d;
            margin-top: 25px;
            margin-bottom: 10px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 20px;
            background: white;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        th {{
            background-color: #3498db;
            color: white;
            font-weight: 600;
        }}
        tr:hover {{
            background-color: #f8f9fa;
        }}
        .label-value {{
            font-family: 'Monaco', 'Menlo', monospace;
            background-color: #f8f9fa;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.9em;
        }}
        .kpi-value {{
            font-weight: bold;
            color: #2c3e50;
        }}
        .kpi-unit {{
            color: #7f8c8d;
            font-style: italic;
            margin-left: 5px;
        }}
        .higher-better {{
            color: #27ae60;
        }}
        .lower-better {{
            color: #e74c3c;
        }}
        .kpi-help {{
            color: #7f8c8d;
            font-style: italic;
            font-size: 0.9em;
        }}
        .test-info {{
            background-color: #ecf0f1;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }}
        .curve-data {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.85em;
            background-color: #f8f9fa;
            padding: 5px;
            border-radius: 3px;
        }}
        .metric-section {{
            margin-bottom: 30px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{escape(report_title)}</h1>

        <div class="test-info">
            <strong>Test Run:</strong> {escape(str(test_info["test_path"]))}<br>
            <strong>Duration:</strong> {escape(str(test_info["duration"]))} |
            <strong>Requests:</strong> {escape(str(test_info["successful_requests"]))}/{escape(str(test_info["total_requests"]))} successful
        </div>

        <h2>🏷️ Test Conditions</h2>
        <table>
            <thead>
                <tr>
                    <th>Label</th>
                    <th>Value</th>
                </tr>
            </thead>
            <tbody>
"""

    # Add test condition labels
    for label, value in sorted(test_labels.items()):
        if label != "higher_is_better":  # Skip internal labels
            html += f"""
                <tr>
                    <td>{escape(label.replace("_", " ").title())}</td>
                    <td><span class="label-value">{escape(str(value))}</span></td>
                </tr>
"""

    html += """
            </tbody>
        </table>
"""

    # Add metadata section if there's any metadata
    if metadata and any(v for v in metadata.values() if v):
        html += """
        <h2>📋 Test Metadata</h2>
        <table>
            <thead>
                <tr>
                    <th>Field</th>
                    <th>Value</th>
                </tr>
            </thead>
            <tbody>
"""

        for key, value in metadata.items():
            if value:  # Only show non-empty metadata
                display_key = escape(key.replace("_", " ").title())
                html += f"""
                <tr>
                    <td>{display_key}</td>
                    <td><span class="label-value">{escape(str(value))}</span></td>
                </tr>
"""

        html += """
            </tbody>
        </table>
"""

    html += """
        <div class="metric-section">
            <h2>📊 Key Performance Indicators</h2>
"""

    if scalar_kpis:
        html += """
            <h3>Scalar Metrics</h3>
            <table>
                <thead>
                    <tr>
                        <th>KPI</th>
                        <th>Value</th>
                        <th>Unit</th>
                        <th>Direction</th>
                        <th>Description</th>
                    </tr>
                </thead>
                <tbody>
"""

        for kpi in scalar_kpis:
            direction_class = (
                "higher-better" if kpi["labels"]["higher_is_better"] else "lower-better"
            )
            direction_text = (
                "↑ Higher is better" if kpi["labels"]["higher_is_better"] else "↓ Lower is better"
            )

            # Format the value using decorator format string
            value = kpi["value"]
            if isinstance(value, int | float):
                try:
                    formatted_value = kpi["format"].format(value)
                except (ValueError, KeyError):
                    formatted_value = str(value)
            else:
                formatted_value = str(value)

            html += f"""
                    <tr>
                        <td><strong>{escape(kpi["kpi_id"].replace("guidellm_", "").replace("_", " ").title())}</strong></td>
                        <td class="kpi-value">{escape(str(formatted_value))}</td>
                        <td class="kpi-unit">{escape(str(kpi["unit"]))}</td>
                        <td class="{direction_class}">{escape(direction_text)}</td>
                        <td class="kpi-help">{escape(str(kpi.get("help", "No description")))}</td>
                    </tr>
"""

        html += """
                </tbody>
            </table>
"""

    if curve_kpis:
        html += """
            <h3>2D Metrics (Performance Curves)</h3>
            <table>
                <thead>
                    <tr>
                        <th>KPI</th>
                        <th>Data Points</th>
                        <th>X-Axis</th>
                        <th>Y-Axis</th>
                        <th>Description</th>
                    </tr>
                </thead>
                <tbody>
"""

        for kpi in curve_kpis:
            # Format 2D data points using decorator format strings
            data_points = kpi["value"]
            if data_points and len(data_points) > 0:
                x_format = kpi.get("x_format", "{:.1f}")
                y_format = kpi.get("y_format", "{:.1f}")
                formatted_points = []
                for x, y in data_points[:5]:  # Show first 5 points
                    try:
                        x_str = x_format.format(x)
                        y_str = y_format.format(y)
                        formatted_points.append(f"({x_str}, {y_str})")
                    except (ValueError, TypeError):
                        formatted_points.append(f"({x}, {y})")
                points_text = ", ".join(formatted_points)
                if len(data_points) > 5:
                    points_text += f" ... (+{len(data_points) - 5} more)"
            else:
                points_text = "No data"

            html += f"""
                    <tr>
                        <td><strong>{escape(kpi["kpi_id"].replace("guidellm_", "").replace("_", " ").title())}</strong></td>
                        <td class="curve-data">{escape(points_text)}</td>
                        <td>{escape(str(kpi.get("x_unit", "unknown")))} - {escape(str(kpi.get("x_help", "")))}</td>
                        <td>{escape(str(kpi.get("y_unit", kpi["unit"])))} - {escape(str(kpi.get("y_help", "")))}</td>
                        <td class="kpi-help">{escape(str(kpi.get("help", "No description")))}</td>
                    </tr>
"""

        html += """
                </tbody>
            </table>
"""

    html += """
        </div>

        <footer style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #7f8c8d; text-align: center;">
            Generated by GuideLLM Caliper Plugin
        </footer>
    </div>
</body>
</html>
"""

    return html


def _generate_multi_test_kpi_report(
    records: list[Any],
    output_dir: Path,
    report_title: str = "GuideLLM KPI Report",
    report_number: int | None = None,
) -> str:
    """
    Generate a KPI report for multiple test records.

    Args:
        records: List of unified result records (2 or more)
        output_dir: Directory to write the HTML report
        report_title: Title for the report

    Returns:
        Path to the generated HTML file
    """
    from projects.caliper.engine.kpi import get_kpi_functions, is_curve_kpi
    from projects.guidellm.postprocess.guidellm.parsing import kpis as kpis_module
    from projects.llm_d.postprocess.llm_d.parsing.kpis import GuideLLMKpiHandler

    kpi_functions = get_kpi_functions(kpis_module)

    # Process each record to extract test data
    test_sections = []

    for i, record in enumerate(records, 1):
        logger.info(f"   📋 Processing test {i}/{len(records)}: {record.test_base_path}")

        # Extract test condition labels
        test_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(record)
        base_labels = {**record.distinguishing_labels}
        all_labels = {**base_labels, **test_labels}

        # Compute KPIs for this record
        kpi_data = []

        for kpi_id, kpi_func in kpi_functions.items():
            try:
                value = kpi_func(record)
            except (TypeError, ValueError, KeyError):
                if is_curve_kpi(kpi_func):
                    value = []  # Empty list for failed curve KPIs
                else:
                    value = None  # None for missing/failed scalar KPIs

            # Create structured KPI record using core dataclass
            kpi_record = KpiRecord(
                schema_version="1",
                kpi_id=kpi_id,
                value=value if value is not None else 0,  # KpiRecord requires non-null value
                unit=kpi_func._kpi_unit,
                run_id=record.test_base_path,
                timestamp="",  # Not critical for display
                labels={"higher_is_better": kpi_func._kpi_higher_is_better},
                metadata={
                    "help": kpi_func._kpi_help,
                    "format": getattr(kpi_func, "_kpi_format", "{:.2f}"),
                    "is_curve": is_curve_kpi(kpi_func),
                },
            )

            # Add 2D-specific metadata if applicable
            if is_curve_kpi(kpi_func):
                kpi_record.x_unit = kpi_func._kpi_x_unit
                kpi_record.x_help = kpi_func._kpi_x_help
                kpi_record.y_unit = getattr(kpi_func, "_kpi_y_unit", None) or kpi_func._kpi_unit
                kpi_record.y_help = getattr(kpi_func, "_kpi_y_help", None) or kpi_func._kpi_help
                # Add formatting info to metadata
                kpi_record.metadata["x_format"] = getattr(kpi_func, "_kpi_x_format", "{:.1f}")
                kpi_record.metadata["y_format"] = getattr(kpi_func, "_kpi_y_format", "{:.1f}")

            # Skip null values for display (can't create KpiRecord with None value)
            if value is not None:
                kpi_data.append(kpi_record.to_dict())

        # Extract metadata and test info
        metadata = GuideLLMKpiHandler.extract_metadata(record)
        test_info = _extract_test_info(record)

        test_sections.append(
            {
                "record": record,
                "test_info": test_info,
                "labels": all_labels,
                "kpi_data": kpi_data,
                "metadata": metadata,
                "section_title": f"Test {i}: {record.test_base_path}",
            }
        )

    # Generate multi-test HTML
    html_content = _generate_multi_test_html(
        report_title=report_title,
        test_sections=test_sections,
    )

    # Write to file
    filename = create_report_filename("kpi_summary", report_number, report_title, "html")
    output_file = output_dir / filename
    output_file.write_text(html_content, encoding="utf-8")

    return str(output_file)


def _generate_multi_test_html(
    report_title: str,
    test_sections: list[dict],
) -> str:
    """Generate HTML for multi-test KPI report."""

    html_parts = []

    # HTML header
    html_parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(report_title)}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        .test-section {{
            border: 2px solid #3498db;
            border-radius: 8px;
            margin: 30px 0;
            padding: 25px;
            background: #fdfdfd;
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
            margin-bottom: 30px;
            text-align: center;
        }}
        h2 {{
            color: #34495e;
            margin-top: 30px;
            margin-bottom: 15px;
            border-left: 4px solid #3498db;
            padding-left: 10px;
        }}
        h3 {{
            color: #7f8c8d;
            margin-top: 25px;
            margin-bottom: 10px;
        }}
        .test-header {{
            background: linear-gradient(135deg, #3498db, #2980b9);
            color: white;
            padding: 15px 20px;
            border-radius: 5px;
            margin-bottom: 20px;
        }}
        .overview {{
            background: #e8f4fd;
            padding: 20px;
            border-radius: 5px;
            margin-bottom: 30px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 20px;
            background: white;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        th {{
            background-color: #3498db;
            color: white;
            font-weight: 600;
        }}
        tr:hover {{
            background-color: #f8f9fa;
        }}
        .label-value {{
            font-family: 'Monaco', 'Menlo', monospace;
            background-color: #f8f9fa;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.9em;
        }}
        .kpi-value {{
            font-weight: bold;
            color: #2c3e50;
        }}
        .kpi-unit {{
            color: #7f8c8d;
            font-style: italic;
            margin-left: 5px;
        }}
        .higher-better {{
            color: #27ae60;
        }}
        .lower-better {{
            color: #e74c3c;
        }}
        .kpi-help {{
            color: #7f8c8d;
            font-style: italic;
            font-size: 0.9em;
        }}
        .test-info {{
            background-color: #ecf0f1;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }}
        .curve-data {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.85em;
            background-color: #f8f9fa;
            padding: 5px;
            border-radius: 3px;
        }}
        .metric-section {{
            margin-bottom: 30px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 {escape(report_title)}</h1>

        <div class="overview">
            <h2>📋 Test Run Summary</h2>
            <p><strong>Total Tests:</strong> {len(test_sections)} test configurations processed</p>
            <p><strong>Test Paths:</strong></p>
            <ul>""")

    # Add test paths overview
    for i, section in enumerate(test_sections, 1):
        html_parts.append(
            f"                <li><strong>Test {i}:</strong> {escape(section['test_info']['test_path'])}</li>"
        )

    html_parts.append("""            </ul>
        </div>
    </div>
""")

    # Add section for each test
    for section in test_sections:
        html_parts.append(_generate_test_section_html(section))

    # Footer
    html_parts.append("""
    <div class="container">
        <footer style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #7f8c8d; text-align: center;">
            Generated by GuideLLM Caliper Plugin - Multi-Test KPI Report
        </footer>
    </div>
</body>
</html>""")

    return "".join(html_parts)


def _generate_test_section_html(section: dict) -> str:
    """Generate HTML for a single test section."""
    test_info = section["test_info"]
    labels = section["labels"]
    kpi_data = section["kpi_data"]
    section_title = section["section_title"]

    # Sort KPIs
    scalar_kpis = [kpi for kpi in kpi_data if not kpi.get("is_curve", False)]
    curve_kpis = [kpi for kpi in kpi_data if kpi.get("is_curve", False)]
    scalar_kpis.sort(key=lambda k: k["kpi_id"])
    curve_kpis.sort(key=lambda k: k["kpi_id"])

    html_parts = []

    html_parts.append(f"""
    <div class="container">
        <div class="test-section">
            <div class="test-header">
                <h2>🔬 {escape(section_title)}</h2>
            </div>

            <div class="test-info">
                <strong>Test Run:</strong> {escape(str(test_info["test_path"]))}<br>
                <strong>Duration:</strong> {escape(str(test_info["duration"]))} |
                <strong>Requests:</strong> {escape(str(test_info["successful_requests"]))}/{escape(str(test_info["total_requests"]))} successful
            </div>

            <h3>🏷️ Test Conditions</h3>
            <table>
                <thead>
                    <tr>
                        <th>Label</th>
                        <th>Value</th>
                    </tr>
                </thead>
                <tbody>""")

    # Add test condition labels
    for label, value in sorted(labels.items()):
        if label != "higher_is_better":  # Skip internal labels
            html_parts.append(f"""
                    <tr>
                        <td>{escape(label.replace("_", " ").title())}</td>
                        <td><span class="label-value">{escape(str(value))}</span></td>
                    </tr>""")

    html_parts.append("""
                </tbody>
            </table>""")

    # Add KPIs
    html_parts.append("""
            <div class="metric-section">
                <h3>📊 Key Performance Indicators</h3>""")

    if scalar_kpis:
        html_parts.append("""
                <h4>Scalar Metrics</h4>
                <table>
                    <thead>
                        <tr>
                            <th>KPI</th>
                            <th>Value</th>
                            <th>Unit</th>
                            <th>Direction</th>
                            <th>Description</th>
                        </tr>
                    </thead>
                    <tbody>""")

        for kpi in scalar_kpis:
            direction_class = (
                "higher-better" if kpi["labels"]["higher_is_better"] else "lower-better"
            )
            direction_text = (
                "↑ Higher is better" if kpi["labels"]["higher_is_better"] else "↓ Lower is better"
            )

            # Format the value
            value = kpi["value"]
            if isinstance(value, int | float):
                try:
                    formatted_value = kpi["format"].format(value)
                except (ValueError, KeyError):
                    formatted_value = str(value)
            else:
                formatted_value = str(value)

            html_parts.append(f"""
                        <tr>
                            <td><strong>{escape(kpi["kpi_id"].replace("guidellm_", "").replace("_", " ").title())}</strong></td>
                            <td class="kpi-value">{escape(str(formatted_value))}</td>
                            <td class="kpi-unit">{escape(str(kpi["unit"]))}</td>
                            <td class="{direction_class}">{escape(direction_text)}</td>
                            <td class="kpi-help">{escape(str(kpi.get("help", "No description")))}</td>
                        </tr>""")

        html_parts.append("""
                    </tbody>
                </table>""")

    if curve_kpis:
        html_parts.append("""
                <h4>2D Metrics (Performance Curves)</h4>
                <table>
                    <thead>
                        <tr>
                            <th>KPI</th>
                            <th>Data Points</th>
                            <th>X-Axis</th>
                            <th>Y-Axis</th>
                            <th>Description</th>
                        </tr>
                    </thead>
                    <tbody>""")

        for kpi in curve_kpis:
            # Format 2D data points
            data_points = kpi["value"]
            if data_points and len(data_points) > 0:
                x_format = kpi.get("x_format", "{:.1f}")
                y_format = kpi.get("y_format", "{:.1f}")
                formatted_points = []
                for x, y in data_points[:5]:  # Show first 5 points
                    try:
                        x_str = x_format.format(x)
                        y_str = y_format.format(y)
                        formatted_points.append(f"({x_str}, {y_str})")
                    except (ValueError, TypeError):
                        formatted_points.append(f"({x}, {y})")
                points_text = ", ".join(formatted_points)
                if len(data_points) > 5:
                    points_text += f" ... (+{len(data_points) - 5} more)"
            else:
                points_text = "No data"

            html_parts.append(f"""
                        <tr>
                            <td><strong>{escape(kpi["kpi_id"].replace("guidellm_", "").replace("_", " ").title())}</strong></td>
                            <td class="curve-data">{escape(points_text)}</td>
                            <td>{escape(str(kpi.get("x_unit", "unknown")))} - {escape(str(kpi.get("x_help", "")))}</td>
                            <td>{escape(str(kpi.get("y_unit", kpi["unit"])))} - {escape(str(kpi.get("y_help", "")))}</td>
                            <td class="kpi-help">{escape(str(kpi.get("help", "No description")))}</td>
                        </tr>""")

        html_parts.append("""
                    </tbody>
                </table>""")

    html_parts.append("""
            </div>
        </div>
    </div>""")

    return "".join(html_parts)
