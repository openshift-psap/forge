"""
HTML report generation utilities for FORGE config review

Contains functions for generating HTML reports and formatting config content.
"""

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def config_to_code_block(config_text: str) -> str:
    """
    Convert YAML config text to HTML code block by escaping HTML and wrapping in <pre><code>.

    Args:
        config_text: Raw YAML config content

    Returns:
        HTML formatted as a code block with YAML syntax highlighting
    """
    # Escape HTML characters
    escaped = config_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Wrap in code block with YAML language hint
    return f'<pre><code class="language-yaml">{escaped}</code></pre>'


def generate_config_html_report(
    queries_and_responses: list,
    config_path: str,
    reference_config_path: str = None,
    output_dir: Path = None,
) -> Path:
    """
    Generate an HTML report for config review analysis

    Args:
        queries_and_responses: List of query/response dictionaries from analysis
        config_path: Path to the analyzed config file
        reference_config_path: Optional path to reference config file
        output_dir: Directory to save the report (defaults to current directory)

    Returns:
        Path to the generated HTML report file
    """
    if output_dir is None:
        output_dir = Path.cwd()

    # Create output filename
    report_filename = "config_review.html"
    report_path = output_dir / report_filename

    # Import clean_content_for_html_report function from report_utils
    from projects.core.agentic_review.report_utils import clean_content_for_html_report

    # Build interactions content
    interactions_html = ""
    for i, interaction in enumerate(queries_and_responses, 1):
        # Clean content by replacing file contents with filenames
        clean_query = clean_content_for_html_report(interaction["query"])
        clean_response = clean_content_for_html_report(interaction["response"])

        # Convert to code blocks for safe display
        query_html = config_to_code_block(clean_query)
        response_html = config_to_code_block(clean_response)

        # Get metadata
        processing_time = interaction.get("processing_time", 0)
        prompt_tokens = interaction.get("prompt_tokens", 0)
        completion_tokens = interaction.get("completion_tokens", 0)
        total_tokens = interaction.get("total_tokens", 0)

        interactions_html += f"""
        <div class="query-interaction">
            <h3>Query {i}: {interaction["query_type"]}</h3>
            <div class="metadata">
                <span class="timestamp">Time: {interaction.get("timestamp", "unknown")}</span>
                <span class="processing-time">Duration: {processing_time:.2f}s</span>
                <span class="tokens">Tokens: {prompt_tokens}+{completion_tokens}={total_tokens}</span>
            </div>

            <div class="query-section">
                <h4 class="query-header" onclick="toggleQuery({i})">
                    <span class="toggle-icon" id="icon-{i}">▶</span>
                    Query:
                </h4>
                <div class="query-content" id="content-{i}" style="display: none;">
                    {query_html}
                </div>
            </div>

            <div class="response-section">
                <h4>Response:</h4>
                {response_html}
            </div>
        </div>
        """

    # Generate summary statistics
    total_queries = len(queries_and_responses)
    total_processing_time = sum(q.get("processing_time", 0) for q in queries_and_responses)
    total_tokens = sum(q.get("total_tokens", 0) for q in queries_and_responses)
    total_prompt_tokens = sum(q.get("prompt_tokens", 0) for q in queries_and_responses)
    total_completion_tokens = sum(q.get("completion_tokens", 0) for q in queries_and_responses)

    # Create reference config section
    reference_section = ""
    if reference_config_path:
        reference_section = f"""
        <div class="reference-config">
            <h3>Reference Configuration</h3>
            <p><strong>File:</strong> {reference_config_path}</p>
            <p>Used for comparison during analysis.</p>
        </div>
        """

    # HTML template
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FORGE Config Review Report - {Path(config_path).name}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f8f9fa;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
        }}
        .header h1 {{ margin: 0; font-size: 2.5em; }}
        .header p {{ margin: 10px 0 0 0; font-size: 1.1em; opacity: 0.9; }}

        .summary {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }}
        .summary h2 {{ margin-top: 0; color: #333; }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }}
        .stat-item {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }}
        .stat-value {{ font-size: 1.8em; font-weight: bold; color: #667eea; }}
        .stat-label {{ font-size: 0.9em; color: #666; }}

        .config-info {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }}
        .config-info h2 {{ margin-top: 0; color: #333; }}

        .reference-config {{
            background: #e8f4fd;
            border: 1px solid #b8daff;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }}

        .query-interaction {{
            background: white;
            margin: 20px 0;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .query-interaction h3 {{
            margin-top: 0;
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }}

        .query-section .query-header {{
            cursor: pointer;
            user-select: none;
            display: flex;
            align-items: center;
            transition: background-color 0.2s ease;
            padding: 5px;
            border-radius: 4px;
            margin-bottom: 10px;
        }}
        .query-section .query-header:hover {{
            background-color: #f8f9fa;
        }}

        .toggle-icon {{
            display: inline-block;
            margin-right: 10px;
            font-size: 12px;
            transition: transform 0.2s ease;
            color: #667eea;
            font-weight: bold;
        }}
        .toggle-icon.expanded {{
            transform: rotate(90deg);
        }}

        .query-content {{
            transition: all 0.3s ease;
            overflow: hidden;
        }}

        .interactions-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        .interactions-header h2 {{
            margin: 0;
            color: #333;
        }}

        .toggle-controls {{
            display: flex;
            gap: 10px;
        }}
        .toggle-btn {{
            background: #667eea;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: background-color 0.2s ease;
        }}
        .toggle-btn:hover {{
            background: #5a6fd8;
        }}
        .toggle-btn:active {{
            background: #4f63d0;
        }}

        .metadata {{
            background: #f8f9fa;
            padding: 10px 15px;
            border-radius: 6px;
            margin: 15px 0;
            font-size: 0.9em;
        }}
        .metadata span {{ margin-right: 20px; color: #666; }}

        .query-section, .response-section {{ margin: 20px 0; }}
        .query-section h4, .response-section h4 {{
            color: #555;
            margin-bottom: 10px;
        }}

        pre {{
            background: #2d3748;
            color: #f7fafc;
            padding: 20px;
            border-radius: 6px;
            overflow-x: auto;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            line-height: 1.4;
            white-space: pre-wrap;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }}
        code {{
            background: #2d3748;
            color: #f7fafc;
        }}

        .footer {{
            text-align: center;
            margin-top: 50px;
            padding: 20px;
            color: #666;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 FORGE Config Review Report</h1>
        <p>Configuration Analysis for {Path(config_path).name}</p>
        <p>Generated on {datetime.now().strftime("%Y-%m-%d at %H:%M:%S")}</p>
    </div>

    <div class="summary">
        <h2>Analysis Summary</h2>
        <div class="stats">
            <div class="stat-item">
                <div class="stat-value">{total_queries}</div>
                <div class="stat-label">Queries</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{total_processing_time:.1f}s</div>
                <div class="stat-label">Total Time</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{total_tokens:,}</div>
                <div class="stat-label">Total Tokens</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{total_prompt_tokens:,}</div>
                <div class="stat-label">Prompt Tokens</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{total_completion_tokens:,}</div>
                <div class="stat-label">Response Tokens</div>
            </div>
        </div>
    </div>

    <div class="config-info">
        <h2>Configuration Details</h2>
        <p><strong>Analyzed File:</strong> {config_path}</p>
        <p><strong>Analysis Date:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
        {reference_section}
    </div>

    <div class="interactions">
        <div class="interactions-header">
            <h2>Detailed Analysis</h2>
            <div class="toggle-controls">
                <button onclick="toggleAllQueries(true)" class="toggle-btn">Show All Queries</button>
                <button onclick="toggleAllQueries(false)" class="toggle-btn">Hide All Queries</button>
            </div>
        </div>
        {interactions_html}
    </div>

    <div class="footer">
        <p>Generated by FORGE Config Review Agent •
        <a href="https://github.com/openshift/forge" target="_blank">Learn more about FORGE</a></p>
    </div>

    <script>
        function toggleQuery(queryId) {{
            const content = document.getElementById(`content-${{queryId}}`);
            const icon = document.getElementById(`icon-${{queryId}}`);

            if (content.style.display === 'none') {{
                content.style.display = 'block';
                icon.textContent = '▼';
                icon.classList.add('expanded');
            }} else {{
                content.style.display = 'none';
                icon.textContent = '▶';
                icon.classList.remove('expanded');
            }}
        }}

        // Optional: Add expand/collapse all functionality
        function toggleAllQueries(expand = null) {{
            const contents = document.querySelectorAll('.query-content');
            const icons = document.querySelectorAll('.toggle-icon');

            contents.forEach((content, index) => {{
                const icon = icons[index];
                if (expand === true || (expand === null && content.style.display === 'none')) {{
                    content.style.display = 'block';
                    icon.textContent = '▼';
                    icon.classList.add('expanded');
                }} else {{
                    content.style.display = 'none';
                    icon.textContent = '▶';
                    icon.classList.remove('expanded');
                }}
            }});
        }}
    </script>
</body>
</html>"""

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        logger.info(f"HTML report generated: {report_path}")
        return report_path

    except Exception as e:
        logger.error(f"Failed to generate HTML report: {e}")
        raise
