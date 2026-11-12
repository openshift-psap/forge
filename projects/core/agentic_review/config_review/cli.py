#!/usr/bin/env python3

"""
CLI interface for FORGE config review

Contains the Click CLI command and all related output formatting functions.
"""

import json
import logging
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)


def format_text_output(result: dict[str, Any]) -> str:
    """
    Format test analysis result for text output

    Args:
        result: Test analysis result dictionary

    Returns:
        Formatted text output
    """
    if result.get("status") != "success":
        error_msg = result.get("error", "Unknown error")
        return f"❌ Test analysis failed: {error_msg}"

    base_artifact_dir = result.get("base_artifact_dir", "unknown")
    structured = result.get("structured_analysis", {})

    output_lines = [f"🤖 Test Analysis Results for: {base_artifact_dir}", "=" * 60, ""]

    # Validation info
    validation = result.get("validation_result", {})
    if validation:
        if validation.get("is_valid", True):
            output_lines.append("✅ Artifact directory is valid")
        else:
            output_lines.append("❌ Artifact validation failed:")
            for error in validation.get("errors", []):
                output_lines.append(f"  - {error}")

        for warning in validation.get("warnings", []):
            output_lines.append(f"⚠️  {warning}")

        structure_info = validation.get("structure_info", {})
        if structure_info:
            output_lines.extend(
                [
                    "",
                    "📊 Artifact Info:",
                    f"  - Has config: {structure_info.get('has_config', False)}",
                    f"  - Has execution engine: {structure_info.get('has_execution_engine', False)}",
                    f"  - Project: {structure_info.get('project', 'unknown')}",
                    f"  - Presets applied: {structure_info.get('preset_count', 0)}",
                    f"  - Config overrides: {structure_info.get('config_override_count', 0)}",
                    "",
                ]
            )

    # Analysis results
    if structured.get("test_description"):
        output_lines.extend(["🎯 What is being tested:", structured["test_description"], ""])

    if structured.get("changes_summary"):
        output_lines.extend(["🔄 Configuration Changes:", structured["changes_summary"], ""])

    if structured.get("testing_focus"):
        output_lines.extend(["📈 Testing Focus:", structured["testing_focus"], ""])

    if structured.get("configuration_context"):
        output_lines.extend(["⚙️ Configuration Context:", structured["configuration_context"], ""])

    # Query metadata
    query_count = result.get("query_count", 0)
    if query_count > 0:
        output_lines.extend([f"🤖 Analysis completed using {query_count} LLM queries", ""])

    return "\n".join(output_lines)


def format_json_output(result: dict[str, Any]) -> str:
    """
    Format test analysis result for JSON output

    Args:
        result: Test analysis result dictionary

    Returns:
        JSON formatted output
    """
    # Create a clean copy for JSON output
    json_result = {
        "status": result.get("status", "unknown"),
        "base_artifact_dir": result.get("base_artifact_dir", "unknown"),
    }

    if result.get("status") == "success":
        json_result.update(
            {
                "validation_result": result.get("validation_result", {}),
                "structured_analysis": result.get("structured_analysis", {}),
                "query_count": result.get("query_count", 0),
            }
        )
    else:
        json_result["error"] = result.get("error", "Unknown error")

    return json.dumps(json_result, indent=2)


@click.command()
@click.option(
    "--base-artifact-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="Path to the FORGE test artifact directory to analyze",
)
@click.option(
    "--output-format",
    type=click.Choice(["json", "text"]),
    default="text",
    help="Output format for results",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Show verbose output during analysis",
)
def cli(
    base_artifact_dir: Path,
    output_format: str,
    verbose: bool,
) -> None:
    """
    FORGE Config Review Agent - Analyze FORGE test configurations

    This agent analyzes FORGE test artifact directories and provides short
    descriptions of what is being tested, focusing on configuration changes.

    Examples:
        # Basic test analysis
        forge config-review --base-artifact-dir /path/to/artifacts

        # JSON output for programmatic use
        forge config-review --base-artifact-dir /path/to/artifacts --output-format json

        # Verbose analysis with detailed analysis
        forge config-review --base-artifact-dir /path/to/artifacts --verbose
    """
    try:
        # Import the main function here to avoid circular imports
        from projects.core.agentic_review.config_review import run_config_review_agent

        logger.info(f"🤖 Starting test analysis for: {base_artifact_dir}")

        # Run the config review agent (vault initialization is handled internally)
        result = run_config_review_agent(base_artifact_dir=base_artifact_dir, verbose=verbose)

        # Format and display output
        if output_format == "json":
            output = format_json_output(result)
        else:
            output = format_text_output(result)

        click.echo(output)

        # Set exit code based on result status
        if result.get("status") == "success":
            if not result.get("validation_result", {}).get("is_valid", True):
                exit(2)  # Config validation failed
            exit(0)
        else:
            exit(1)

    except Exception as e:
        logger.error(f"❌ Config review CLI failed: {e}")
        if output_format == "json":
            error_result = {"status": "error", "error": str(e)}
            click.echo(json.dumps(error_result, indent=2))
        else:
            click.echo(f"❌ Config review failed: {e}")
        exit(1)


if __name__ == "__main__":
    cli()
