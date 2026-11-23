"""
Generic Analysis Utilities for FORGE Agentic Processing

This module contains utilities for processing and analyzing LLM responses
and other analysis data that can be used across different agentic workflows.
"""

import re


def extract_structured_analysis(analysis_text: str) -> dict:
    """
    Extract structured analysis from LLM response

    Args:
        analysis_text: Raw LLM response text

    Returns:
        Dictionary with extracted structured elements
    """
    extracted = {
        "root_cause": "",
        "failed_step": "",
        "trigger": "",
        "raw_analysis": analysis_text,
    }

    # Define patterns to match the 5-point structure
    patterns = {
        "root_cause": [
            r"(?:1\.|•|\*)\s*\*\*Root Cause\*\*:?\s*(.+?)(?=(?:2\.|•|\*)\s*\*\*|$)",
            r"\*\*Root Cause\*\*:?\s*(.+?)(?=\*\*|$)",
            r"Root Cause:?\s*(.+?)(?=Failed Step|Trigger|Fix|Prevent|$)",
        ],
        "failed_step": [
            r"(?:2\.|•|\*)\s*\*\*Failed Step\*\*:?\s*(.+?)(?=(?:3\.|•|\*)\s*\*\*|$)",
            r"\*\*Failed Step\*\*:?\s*(.+?)(?=\*\*|$)",
            r"Failed Step:?\s*(.+?)(?=Root Cause|Trigger|Fix|Prevent|$)",
        ],
        "trigger": [
            r"(?:3\.|•|\*)\s*\*\*Trigger\*\*:?\s*(.+?)(?=(?:4\.|•|\*)\s*\*\*|$)",
            r"\*\*Trigger\*\*:?\s*(.+?)(?=\*\*|$)",
            r"Trigger:?\s*(.+?)(?=Root Cause|Failed Step|Fix|Prevent|$)",
        ],
    }

    # Try to extract each field using multiple patterns
    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            match = re.search(pattern, analysis_text, re.DOTALL | re.IGNORECASE)
            if match:
                # Clean up the extracted text
                value = match.group(1).strip()
                # Remove extra whitespace and newlines
                value = re.sub(r"\s+", " ", value)
                # Remove trailing punctuation patterns
                value = re.sub(r"\s*(?:\n|$)", "", value)
                extracted[field] = value
                break

    return extracted
