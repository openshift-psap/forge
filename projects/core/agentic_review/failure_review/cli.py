#!/usr/bin/env python3

"""
CLI interface for FORGE failure review using pure NOOA agents

This module provides direct access to NOOA agents without compatibility wrappers.
Users interact with agents directly for maximum flexibility and performance.
"""

# Add project root to path when run directly (must be before other imports)
import sys
from pathlib import Path

if __name__ == "__main__":
    # Add the project root (5 levels up from this file) to Python path
    # projects.core.agentic_review.failure_review/cli.py -> /home/kpouget/openshift/forge-censoring
    project_root = Path(__file__).parent.parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import json
import logging

import click
from projects.core.agentic_review.review.models import create_llm_client, load_model_config

from projects.core.agentic_review.artifact_processing import (
    find_failure_files,
    list_all_files_in_artifact_dir,
    read_failure_and_log,
)
from projects.core.library import vault

# Handle both relative and direct imports
try:
    from .agent import FailureContext, FailureReviewAgent
except ImportError:
    from projects.core.agentic_review.failure_review.agent import FailureContext, FailureReviewAgent

logger = logging.getLogger(__name__)

DEFAULT_MODEL_KEY = "qwen-3-6-35b"


def format_analysis_output(analyses: list, base_artifact_dir: str, output_format: str) -> str:
    """
    Format NOOA agent analysis results for output

    Args:
        analyses: List of FailureAnalysis objects from NOOA agents
        base_artifact_dir: Base artifact directory path
        output_format: Output format ("text" or "json")

    Returns:
        Formatted output string
    """
    if output_format == "json":
        return _format_json_output(analyses, base_artifact_dir)
    else:
        return _format_text_output(analyses, base_artifact_dir)


def _format_text_output(analyses: list, base_artifact_dir: str) -> str:
    """Format analysis results for text output"""
    output_lines = [
        f"🤖 NOOA Failure Analysis Results for: {base_artifact_dir}",
        "=" * 60,
        "",
        f"📊 Analyzed {len(analyses)} failures",
        "",
    ]

    for i, analysis in enumerate(analyses, 1):
        output_lines.append(f"🔍 Failure #{i}: {analysis.category}")
        output_lines.append(
            f"  📁 Directory: {Path(analysis.timeline or '').name}"
        )  # Using timeline field as temp storage
        output_lines.append(f"  ⚠️  Severity: {analysis.severity.upper()}")
        output_lines.append(f"  🔍 Root Cause: {analysis.root_cause}")
        output_lines.append(f"  ❌ Failed Step: {analysis.failed_step}")
        output_lines.append("")
        output_lines.append(f"📝 Summary: {analysis.summary}")
        output_lines.append("")

        if analysis.technical_details:
            output_lines.append("🔧 Technical Details:")
            # Truncate technical details for readability
            details_lines = analysis.technical_details.split("\n")[:10]
            output_lines.extend([f"  {line}" for line in details_lines])
            if len(analysis.technical_details.split("\n")) > 10:
                output_lines.append("  ...")
            output_lines.append("")

        if i < len(analyses):
            output_lines.append("=" * 60)

    return "\n".join(output_lines)


def _format_json_output(analyses: list, base_artifact_dir: str) -> str:
    """Format analysis results for JSON output"""
    result = {
        "status": "success",
        "agent_type": "nooa",
        "artifact_dir": base_artifact_dir,
        "failures_analyzed": len(analyses),
        "analyses": [],
    }

    for analysis in analyses:
        # Convert FailureAnalysis dataclass to dict
        analysis_dict = {
            "root_cause": analysis.root_cause,
            "failed_step": analysis.failed_step,
            "category": analysis.category,
            "severity": analysis.severity,
            "summary": analysis.summary,
            "technical_details": analysis.technical_details,
            "timeline": analysis.timeline,
            "configuration_issues": analysis.configuration_issues,
            "recommended_actions": analysis.recommended_actions,
        }
        result["analyses"].append(analysis_dict)

    return json.dumps(result, indent=2)


@click.command()
@click.option(
    "--base-artifact-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="Path to the base artifact directory to analyze",
)
@click.option(
    "--output-format",
    type=click.Choice(["json", "text"]),
    default="text",
    help="Output format for results",
)
@click.option("--verbose", is_flag=True, help="Show verbose output during analysis")
@click.option("--model-key", default=DEFAULT_MODEL_KEY, help="Model key to use for analysis")
def cli(base_artifact_dir: Path, output_format: str, verbose: bool, model_key: str):
    """
    FORGE Failure Review CLI - Direct NOOA agent analysis

    This tool uses NOOA agents directly to analyze FORGE test failures.
    Each failure is analyzed by a dedicated agent instance providing
    structured, typed results.

    Examples:
        # Basic failure analysis
        forge failure-review --base-artifact-dir /path/to/artifacts

        # JSON output for programmatic use
        forge failure-review --base-artifact-dir /path/to/artifacts --output-format json

        # Verbose analysis with detailed progress
        forge failure-review --base-artifact-dir /path/to/artifacts --verbose
    """
    # Set up logging - always show INFO for LLM calls
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        # Enable debug logging for HTTP requests
        logging.getLogger("requests").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
    else:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    try:
        logger.info(f"🤖 Starting NOOA failure analysis for: {base_artifact_dir}")

        # Initialize vault manager with fallback for development
        try:
            if verbose:
                click.echo("Initializing vault manager...")
            vault.init(vaults=["psap-models-corp-rh"])

            # Load model configuration
            if verbose:
                click.echo(f"Loading model configuration for: {model_key}")

            models_config = load_model_config("psap-models-corp-rh", "agent-models.yaml")
            model_config = models_config.get(model_key)

            if not model_config:
                raise ValueError(f"Model key '{model_key}' not found in vault configuration")

            # Create NOOA LLM client
            if verbose:
                click.echo(f"Creating NOOA client for model: {model_config.get('model_id')}")

            llm = create_llm_client(model_config)

        except Exception as vault_error:
            # Vault failed - try to create LLM with forge defaults
            if verbose:
                click.echo(f"⚠️ Vault initialization failed: {vault_error}")
                click.echo("Creating LLM with default forge config...")

            # Create default forge model config with correct field names
            model_config = {
                "model_id": model_key,
                "model_api": "https://forge-placeholder.api.com",
                "user_key": "sk-forge-placeholder-key",
            }
            llm = create_llm_client(model_config)

        # Find failure files
        failure_files = find_failure_files(base_artifact_dir)
        if not failure_files:
            click.echo("No FAILURE files found in the artifact directory.")
            exit(0)

        if verbose:
            click.echo(f"Found {len(failure_files)} failure files to analyze")

        # Create NOOA agent
        agent = FailureReviewAgent(llm=llm)
        analyses = []

        # Analyze each failure using NOOA agent
        for i, failure_file in enumerate(failure_files, 1):
            if verbose:
                click.echo(
                    f"Analyzing failure {i}/{len(failure_files)}: {failure_file.parent.name}"
                )

            logger.debug(f"🔍 Starting analysis of failure file: {failure_file}")

            # Read failure data
            logger.debug("📖 Reading failure data and logs...")
            failure_data = read_failure_and_log(failure_file)
            logger.debug(
                f"📖 Read {len(failure_data['failure_content'])} chars of failure content, {len(failure_data.get('log_content', ''))} chars of log content"
            )

            # Create context for the agent
            context = FailureContext(
                failure_file=failure_file,
                base_artifact_dir=base_artifact_dir,
                failure_content=failure_data["failure_content"],
                log_content=failure_data["log_content"],
                agent_md_content=failure_data.get("agent_md_content", ""),
                available_files=list_all_files_in_artifact_dir(failure_file.parent),
            )

            # Use NOOA agent to analyze the failure
            logger.debug("🤖 Starting LLM agent analysis...")
            analysis = agent.analyze_failure(context, verbose=verbose)
            logger.debug("✅ LLM agent analysis completed")

            # Store failure directory in timeline field temporarily for output formatting
            analysis.timeline = str(failure_file.parent)

            analyses.append(analysis)
            logger.debug(f"📊 Completed analysis {i}/{len(failure_files)}")

        # Format and display results
        output = format_analysis_output(analyses, str(base_artifact_dir), output_format)
        click.echo(output)

        logger.info(f"✅ Successfully analyzed {len(analyses)} failures")
        exit(0)

    except ImportError as e:
        error_msg = "❌ NOOA package not available. Install with: pip install nooa"
        logger.error(error_msg)

        if output_format == "json":
            error_result = {"status": "error", "error": str(e), "agent_type": "nooa"}
            click.echo(json.dumps(error_result, indent=2))
        else:
            click.echo(error_msg)
        exit(1)

    except Exception as e:
        import traceback

        logger.error(f"❌ Failure analysis failed: {e}")
        logger.error("Full traceback:")
        traceback.print_exc()

        if output_format == "json":
            error_result = {
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc(),
                "agent_type": "nooa",
            }
            click.echo(json.dumps(error_result, indent=2))
        else:
            click.echo(f"❌ Failure analysis failed: {e}")
            click.echo("\nFull traceback:")
            click.echo(traceback.format_exc())
        exit(1)


if __name__ == "__main__":
    cli()
