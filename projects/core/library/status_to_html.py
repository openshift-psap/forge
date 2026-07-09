"""Convert Caliper postprocess status YAML to HTML report."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def convert_status_yaml_to_html(
    yaml_file_path: Path | str, output_html_path: Path | str | None = None
) -> str:
    """Convert a Caliper postprocess status YAML file to HTML report.

    Args:
        yaml_file_path: Path to the postprocess_status.yaml file
        output_html_path: Path for the output HTML file. If None, uses same directory as YAML with .html extension

    Returns:
        Path to the generated HTML file

    Example:
        convert_status_yaml_to_html("~/kserve/caliper/postprocess_status.yaml")
    """
    yaml_path = Path(yaml_file_path).expanduser().resolve()

    if not yaml_path.exists():
        raise FileNotFoundError(f"Status YAML file not found: {yaml_path}")

    # Determine output HTML path
    if output_html_path is None:
        output_html_path = yaml_path.parent / f"{yaml_path.stem}_report.html"
    else:
        output_html_path = Path(output_html_path).expanduser().resolve()

    # Read and parse the YAML file
    try:
        with open(yaml_path, encoding="utf-8") as f:
            status_data = yaml.safe_load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse YAML file {yaml_path}: {e}") from e

    # Extract status information
    final_status = status_data.get("final_status", "unknown")
    test_phase = status_data.get("test_phase", {})
    steps_raw = status_data.get("steps", [])
    base_directory = status_data.get("base_directory")

    # Convert steps to iterable format for processing
    steps_list = []
    if isinstance(steps_raw, list):
        # New list-based format with {step_name: step_data} structure
        steps_list = steps_raw
    elif isinstance(steps_raw, dict):
        # Legacy dict format - convert to new list format
        steps_list = [{name: step_data} for name, step_data in steps_raw.items()]

    # Determine overall status styling
    if final_status in ["passed", "success"]:
        status_card_class = "success"
        status_badge_class = "status-passed"
    elif final_status == "failed":
        status_card_class = "failed"
        status_badge_class = "status-failed"
    else:
        status_card_class = "warning"
        status_badge_class = "status-warning"

    # Count step statuses for summary
    step_counts = {"success": 0, "failed": 0, "skipped": 0, "warning": 0}
    for step_dict in steps_list:
        for _step_name, step_info in step_dict.items():
            step_status = step_info.get("status", "unknown")
            if step_status in ["ok", "success"]:
                step_counts["success"] += 1
            elif step_status == "failed":
                step_counts["failed"] += 1
            elif step_status == "skipped":
                step_counts["skipped"] += 1
            elif step_status == "warning":
                step_counts["warning"] += 1

    # Generate HTML
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Caliper Postprocess Status Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            border-bottom: 3px solid #4CAF50;
            padding-bottom: 10px;
            margin-bottom: 30px;
        }}
        .status-header {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 30px;
        }}
        .status-card {{
            padding: 20px;
            border-radius: 8px;
            border-left: 4px solid #6c757d;
            background-color: #f8f9fa;
        }}
        .status-card.success {{ border-left-color: #28a745; background-color: #f8fff9; }}
        .status-card.failed {{ border-left-color: #dc3545; background-color: #fff8f8; }}
        .status-card.warning {{ border-left-color: #ffc107; background-color: #fffdf7; }}
        .status-badge {{
            display: inline-block;
            padding: 6px 16px;
            border-radius: 20px;
            font-weight: bold;
            text-transform: uppercase;
            font-size: 1em;
        }}
        .status-passed {{ background-color: #28a745; color: white; }}
        .status-failed {{ background-color: #dc3545; color: white; }}
        .status-warning {{ background-color: #ffc107; color: #212529; }}
        .summary-stats {{
            display: flex;
            gap: 12px;
            margin-top: 15px;
        }}
        .stat {{
            background-color: #fff;
            padding: 6px 10px;
            border-radius: 4px;
            border: 1px solid #ddd;
            font-size: 0.85em;
            font-weight: 500;
        }}
        .stat.success {{ border-color: #28a745; color: #28a745; }}
        .stat.failed {{ border-color: #dc3545; color: #dc3545; }}
        .stat.warning {{ border-color: #ffc107; color: #856404; }}
        .stat.skipped {{ border-color: #6c757d; color: #6c757d; }}
        .step-card {{
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            margin-bottom: 20px;
            overflow: hidden;
        }}
        .step-header {{
            padding: 15px 20px;
            background-color: #f8f9fa;
            border-bottom: 1px solid #e0e0e0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .step-header.success {{ background-color: #f8fff9; border-left: 4px solid #28a745; }}
        .step-header.failed {{ background-color: #fff8f8; border-left: 4px solid #dc3545; }}
        .step-header.skipped {{ background-color: #f8f9fa; border-left: 4px solid #6c757d; }}
        .step-header.warning {{ background-color: #fffdf7; border-left: 4px solid #ffc107; }}
        .step-title {{
            font-size: 1.1em;
            font-weight: 600;
            margin: 0;
        }}
        .step-status {{
            padding: 4px 12px;
            border-radius: 15px;
            font-size: 0.85em;
            font-weight: bold;
            text-transform: uppercase;
        }}
        .step-ok {{ background-color: #d4edda; color: #155724; }}
        .step-failed {{ background-color: #f8d7da; color: #721c24; }}
        .step-skipped {{ background-color: #e2e3e5; color: #495057; }}
        .step-warning {{ background-color: #fff3cd; color: #856404; }}
        .step-content {{
            padding: 20px;
        }}
        .detail-row {{
            display: flex;
            margin-bottom: 8px;
        }}
        .detail-label {{
            font-weight: 600;
            min-width: 120px;
            color: #555;
        }}
        .detail-value {{
            color: #333;
        }}
        .files-section {{
            margin-top: 15px;
        }}
        .files-section h4 {{
            margin: 0 0 10px 0;
            color: #555;
            font-size: 0.95em;
        }}
        .file-links {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .file-links a {{
            padding: 6px 12px;
            background-color: #e3f2fd;
            color: #1976d2;
            text-decoration: none;
            border-radius: 4px;
            font-size: 0.9em;
            border: 1px solid #bbdefb;
        }}
        .error-box {{
            background-color: #fff5f5;
            border: 1px solid #feb2b2;
            border-radius: 4px;
            padding: 12px;
            margin-top: 15px;
            font-size: 0.9em;
            color: #c53030;
        }}
        .source-info {{
            margin-top: 30px;
            padding: 15px;
            background-color: #f8f9fa;
            border-radius: 4px;
            font-size: 0.9em;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Caliper Postprocess Status Report</h1>

        <div class="status-header">
            <div class="status-card {status_card_class}">
                <h3 style="margin-top: 0;">Final Status</h3>
                <span class="status-badge {status_badge_class}">{final_status}</span>
                <div class="summary-stats">
                    <div class="stat success">✅ {step_counts["success"]} Success</div>
                    <div class="stat failed">❌ {step_counts["failed"]} Failed</div>
                    <div class="stat warning">⚠️ {step_counts["warning"]} Warning</div>
                    <div class="stat skipped">⏭️ {step_counts["skipped"]} Skipped</div>
                </div>
            </div>
            <div class="status-card">
                <h3 style="margin-top: 0;">Test Phase</h3>
                <p style="margin: 10px 0;"><strong>{test_phase.get("phase", "unknown")}</strong></p>
                {f'<p style="font-size: 0.9em; margin: 0; color: #666;">{test_phase.get("message", "")}</p>' if test_phase.get("message") else ""}
            </div>
        </div>

        <h2>Processing Steps</h2>"""

    # Generate step cards
    step_order = [
        "parse",
        "visualize",
        "artifacts_to_kpis",
        "kpis_to_csv",
        "artifacts_to_ai_data",
        "s3_import",
        "analyse_kpis",
        "s3_export",
    ]

    # Create step lookup for easy access
    step_lookup = {}
    for step_dict in steps_list:
        for step_name, step_data in step_dict.items():
            step_lookup[step_name] = step_data

    for step_name in step_order:
        if step_name not in step_lookup:
            continue

        step_info = step_lookup[step_name]
        step_status = step_info.get("status", "unknown")

        # Format step name
        step_display_map = {
            "artifacts_to_kpis": "KPI Generation",
            "kpis_to_csv": "KPI CSV Export",
            "artifacts_to_ai_data": "AI Data Export",
            "s3_import": "S3 Import",
            "s3_export": "S3 Export",
            "analyse_kpis": "KPI Analysis",
        }
        step_display = step_display_map.get(step_name, step_name.replace("_", " ").title())

        # Determine status styling
        if step_status in ["ok", "success"]:
            header_class = "success"
            status_class = "step-ok"
            status_text = "SUCCESS"
        elif step_status == "failed":
            header_class = "failed"
            status_class = "step-failed"
            status_text = "FAILED"
        elif step_status == "skipped":
            header_class = "skipped"
            status_class = "step-skipped"
            status_text = "SKIPPED"
        elif step_status == "warning":
            header_class = "warning"
            status_class = "step-warning"
            status_text = "WARNING"
        else:
            header_class = "skipped"
            status_class = "step-skipped"
            status_text = step_status.upper()

        html_content += f"""
        <div class="step-card">
            <div class="step-header {header_class}">
                <div class="step-title">{step_display}</div>
                <span class="step-status {status_class}">{status_text}</span>
            </div>
            <div class="step-content">"""

        # Add step details
        details = []

        if step_name == "parse" and step_status in ["ok", "success"]:
            record_count = step_info.get("record_count", 0)
            plugin_module = step_info.get("plugin_module", "unknown")
            parse_cache_ref = step_info.get("parse_cache_ref", "")
            details.extend(
                [
                    ("Records", str(record_count)),
                    ("Plugin", plugin_module),
                    ("Cache", parse_cache_ref) if parse_cache_ref else None,
                ]
            )

        elif step_name == "visualize" and step_status in ["ok", "success"]:
            output_files = step_info.get("output_files", [])
            plugin_module = step_info.get("plugin_module", "")
            details.extend(
                [
                    ("Files Generated", str(len(output_files))),
                    ("Plugin", plugin_module) if plugin_module else None,
                ]
            )

        elif step_name == "artifacts_to_kpis" and step_status in ["ok", "success"]:
            kpi_count = step_info.get("kpi_count", 0)
            output_file = step_info.get("output_file", "")
            details.extend([("KPI Records", str(kpi_count))])

        elif step_name == "artifacts_to_ai_data" and step_status in ["ok", "success"]:
            exported_entries = step_info.get("exported_entries", 0)
            details.extend([("Exported Entries", str(exported_entries))])

        elif step_name == "s3_import" and step_status in ["ok", "success"]:
            downloaded_files = step_info.get("downloaded_files", 0)
            failed_files = step_info.get("failed_files", 0)
            details.extend(
                [
                    ("Downloaded Files", str(downloaded_files)),
                    ("Failed Files", str(failed_files)) if failed_files > 0 else None,
                ]
            )

        elif step_name == "analyse_kpis" and step_status in ["ok", "success"]:
            baseline_files_count = step_info.get("baseline_files_count", 0)
            details.extend([("Baseline Files", str(baseline_files_count))])

        elif step_name == "s3_export" and step_status in ["ok", "success"]:
            uploaded_files = step_info.get("uploaded_files", 0)
            failed_files = step_info.get("failed_files", 0)
            exported_path = step_info.get("exported_path", "")
            details.extend(
                [
                    ("Uploaded Files", str(uploaded_files)),
                    ("Failed Files", str(failed_files)) if failed_files > 0 else None,
                    ("Export Path", exported_path) if exported_path else None,
                ]
            )

        # Add failure/skip reasons
        reason = step_info.get("reason", "")
        error = step_info.get("error", "")

        if reason and step_status == "skipped":
            details.append(("Reason", reason))

        # Display details
        for detail in details:
            if detail is None:
                continue
            label, value = detail
            html_content += f"""
                <div class="detail-row">
                    <div class="detail-label">{label}:</div>
                    <div class="detail-value">{value}</div>
                </div>"""

        # Add error details if present
        if error:
            html_content += f'<div class="error-box"><strong>Error:</strong> {error}</div>'

        detail = step_info.get("detail", "")
        if detail and step_status == "failed":
            html_content += f'<div class="error-box"><strong>Details:</strong> {detail}</div>'

        # Add file links
        file_links = []

        def _get_full_path(relative_path: str) -> str:
            """Convert relative path to full path using base_directory."""
            if base_directory and relative_path:
                return str(Path(base_directory) / relative_path)
            return relative_path

        if step_name == "visualize" and step_status in ["ok", "success"]:
            output_files = step_info.get("output_files", [])

            for path in output_files[:8]:  # Show first 8 files
                full_path = _get_full_path(path)
                file_name = Path(path).name
                file_links.append((file_name, full_path))

            if len(output_files) > 8:
                file_links.append((f"... and {len(output_files) - 8} more files", ""))

        elif step_name == "artifacts_to_kpis" and step_status in ["ok", "success"]:
            output_file = step_info.get("output_file", "")
            if output_file:
                full_path = _get_full_path(output_file)
                file_name = Path(output_file).name
                file_links.append((f"📈 {file_name}", full_path))

        elif step_name == "kpis_to_csv" and step_status in ["ok", "success"]:
            output_file = step_info.get("output_file", "")
            if output_file:
                full_path = _get_full_path(output_file)
                file_name = Path(output_file).name
                file_links.append((f"📊 {file_name}", full_path))

        elif step_name == "artifacts_to_ai_data" and step_status in ["ok", "success"]:
            output_file = step_info.get("output_file", "")
            if output_file:
                full_path = _get_full_path(output_file)
                file_name = Path(output_file).name
                file_links.append((f"🤖 {file_name}", full_path))

        elif step_name == "analyse_kpis" and step_status in ["ok", "success"]:
            output_file = step_info.get("output_file", "")
            if output_file:
                full_path = _get_full_path(output_file)
                file_name = Path(output_file).name
                file_links.append((f"📊 {file_name}", full_path))

        if file_links:
            html_content += (
                '<div class="files-section"><h4>Generated Files:</h4><div class="file-links">'
            )
            for link_text, link_path in file_links:
                if link_path:
                    html_content += f'<a href="file://{link_path}">{link_text}</a>'
                else:
                    html_content += f'<span style="color: #666;">{link_text}</span>'
            html_content += "</div></div>"

        html_content += """
            </div>
        </div>"""

    # Close HTML
    html_content += f"""
        <div class="source-info">
            <strong>Report Generated:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>
            <strong>Source YAML:</strong> {yaml_path}<br>
            <strong>HTML Report:</strong> {output_html_path}
        </div>
    </div>
</body>
</html>"""

    # Write HTML file
    output_html_path.parent.mkdir(parents=True, exist_ok=True)
    output_html_path.write_text(html_content, encoding="utf-8")

    logger.info(f"Generated HTML report: {output_html_path}")
    return str(output_html_path)
