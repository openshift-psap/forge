"""
HTML report generation utilities for FORGE failure analysis

Contains functions for generating HTML reports and formatting text content.
"""

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def text_to_code_block(text: str) -> str:
    """
    Convert text to HTML code block by escaping HTML and wrapping in <pre><code>.

    Args:
        text: Raw text content

    Returns:
        HTML formatted as a code block
    """
    # Escape HTML characters
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Wrap in code block
    return f"<pre><code>{escaped}</code></pre>"


def generate_html_report(
    queries_and_responses: list, base_artifact_dir: Path, failure_dir: str
) -> str:
    """
    Generate HTML report with all queries and responses using external template

    Args:
        queries_and_responses: List of {query, response} dictionaries
        base_artifact_dir: Base artifact directory to save the report
        failure_dir: Failure directory name for the report title

    Returns:
        Path to the generated HTML file
    """

    # Load HTML template
    template_path = Path(__file__).parent / "report_template.html"

    try:
        with open(template_path, encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        logger.error(f"HTML template not found: {template_path}")
        # Fallback to simple HTML if template is missing
        template = """<!DOCTYPE html>
<html>
<head><title>FORGE Failure Analysis Report</title></head>
<body>
<h1>🤖 FORGE Failure Analysis Report</h1>
<p><strong>📁 Failure Directory:</strong> {failure_dir}</p>
<p><strong>🕐 Generated:</strong> {generation_time}</p>
<p><strong>🔍 Total Interactions:</strong> {interaction_count}</p>
{interactions_content}
</body>
</html>"""

    # Import clean_content_for_html_report function from report_utils
    from projects.core.agentic_review.report_utils import clean_content_for_html_report

    # Build interactions content
    interactions_html = ""
    for i, interaction in enumerate(queries_and_responses, 1):
        # Clean content by replacing file contents with filenames
        clean_query = clean_content_for_html_report(interaction["query"])
        clean_response = clean_content_for_html_report(interaction["response"])

        # Convert to code blocks for safe display
        query_html = text_to_code_block(clean_query)
        response_html = text_to_code_block(clean_response)

        # Get query type for better organization (for multi-query approach)
        query_type = interaction.get("query_type", f"Query #{i}")

        # Build metadata display
        processing_time = interaction.get("processing_time", 0)
        prompt_tokens = interaction.get("prompt_tokens", 0)
        completion_tokens = interaction.get("completion_tokens", 0)
        total_tokens = interaction.get("total_tokens", 0)
        prompt_length = interaction.get("prompt_length", 0)
        response_length = interaction.get("response_length", 0)

        query_metadata = f"""
            <div class="query-metadata">
                <span class="metadata-item">📏 <span class="metadata-label">Prompt:</span> {prompt_length} chars</span>
                <span class="metadata-item">🪙 <span class="metadata-label">Tokens:</span> {prompt_tokens} in</span>
            </div>"""

        # Build file tracking metadata
        files_consumed = interaction.get("files_consumed", [])
        files_available = interaction.get("files_available", [])
        files_requested = interaction.get("files_requested", [])

        file_metadata_html = ""
        if files_consumed:
            file_metadata_html += f"""
                <div class="file-metadata">
                    <span class="metadata-item">📄 <span class="metadata-label">Files Consumed:</span> {len(files_consumed)} files</span>
                </div>"""

        if files_available:
            file_metadata_html += f"""
                <div class="file-metadata">
                    <span class="metadata-item">📂 <span class="metadata-label">Files Available:</span> {len(files_available)} files</span>
                </div>"""

        if files_requested:
            file_metadata_html += f"""
                <div class="file-metadata">
                    <span class="metadata-item">📋 <span class="metadata-label">Additional Files Requested:</span> {len(files_requested)} requests</span>
                </div>"""

        response_metadata = f"""
            <div class="query-metadata">
                <span class="metadata-item">📏 <span class="metadata-label">Response:</span> {response_length} chars</span>
                <span class="metadata-item">🪙 <span class="metadata-label">Tokens:</span> {completion_tokens} out ({total_tokens} total)</span>
                <span class="metadata-item">⏱️ <span class="metadata-label">Time:</span> {processing_time:.2f}s</span>
            </div>
            {file_metadata_html}"""

        interactions_html += f"""
        <div class="interaction">
            <div class="query collapsible-header collapsed">
                <h3>🤖 {query_type}</h3>
                <div class="timestamp">{interaction.get("timestamp", "Unknown time")}</div>
                {query_metadata}
            </div>
            <div class="content collapsible-content collapsed">{query_html}</div>

            <div class="response">
                <h3>💬 {query_type} - Response</h3>
                {response_metadata}
            </div>
            <div class="content">{response_html}</div>
        </div>"""

    # Build file consumption summary
    all_consumed_files = set()
    all_available_files = set()
    all_requested_files = set()

    for interaction in queries_and_responses:
        if interaction.get("files_consumed"):
            all_consumed_files.update(interaction["files_consumed"])
        if interaction.get("files_available"):
            all_available_files.update(interaction["files_available"])
        if interaction.get("files_requested"):
            all_requested_files.update(interaction["files_requested"])

    # Build file summary section
    file_summary_html = f"""
        <div class="file-summary">
            <h2>📁 File Consumption Summary</h2>

            <div class="file-category">
                <h3>📄 Files Directly Analyzed ({len(all_consumed_files)} files)</h3>
                <div class="file-list">
    """

    for file_path in sorted(all_consumed_files):
        file_summary_html += f'<div class="file-item">• {file_path}</div>\n'

    file_summary_html += f"""
                </div>
            </div>

            <div class="file-category">
                <div class="collapsible-header collapsed" style="background: #3498db; color: white; padding: 10px; cursor: pointer; border-radius: 4px; margin-bottom: 0;">
                    <h3 style="margin: 0;">📂 Files Available to LLM ({len(all_available_files)} files)</h3>
                    <small>Click to expand/collapse list</small>
                </div>
                <div class="file-list collapsible-content collapsed">
    """

    for file_path in sorted(all_available_files):
        file_summary_html += f'<div class="file-item">• {file_path}</div>\n'

    file_summary_html += """
                </div>
            </div>
    """

    if all_requested_files:
        file_summary_html += f"""
            <div class="file-category">
                <h3>📋 Additional Files Requested by LLM ({len(all_requested_files)} requests)</h3>
                <div class="file-list">
        """

        for file_request in sorted(all_requested_files):
            file_summary_html += f'<div class="file-item">• {file_request}</div>\n'

        file_summary_html += """
                </div>
            </div>
        """

    file_summary_html += """
        </div>
    """

    # Fill in template variables using string replacement to avoid CSS brace conflicts
    html_content = template.replace("{failure_dir}", failure_dir)
    html_content = html_content.replace(
        "{generation_time}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    html_content = html_content.replace("{interaction_count}", str(len(queries_and_responses)))
    html_content = html_content.replace("{interactions_content}", interactions_html)

    # Add file summary if template supports it, otherwise append before closing body tag
    if "{file_summary}" in html_content:
        html_content = html_content.replace("{file_summary}", file_summary_html)
    else:
        # Fallback: add before closing body tag
        html_content = html_content.replace("</body>", f"{file_summary_html}\n</body>")

    # Save HTML file with consistent name (overwrite existing)
    html_filename = "failure_analysis_report.html"
    html_path = base_artifact_dir / html_filename

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return str(html_path)
