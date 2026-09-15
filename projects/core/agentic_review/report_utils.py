"""
Generic Report Utilities for FORGE Agentic Processing

This module contains utilities for generating reports and formatting content
that can be used across different agentic workflows.
"""

import re


def clean_content_for_html_report(text: str) -> str:
    """
    Clean content for HTML report by replacing file contents with filenames

    Args:
        text: Original text with potential file contents

    Returns:
        Cleaned text with file contents replaced by filenames
    """
    # Replace file content sections with just filenames
    # Pattern: ### filename:\n```\n[content]\n```
    pattern = r"### ([^:]+):\s*\n```\s*\n.*?\n```"

    def replace_file_content(match):
        filename = match.group(1)
        return f"### {filename}:\n`[File content - see {filename}]`"

    # Use DOTALL flag to match across newlines
    cleaned_text = re.sub(pattern, replace_file_content, text, flags=re.DOTALL)

    return cleaned_text
