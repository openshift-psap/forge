#!/usr/bin/env python3

"""
CLI interface for FORGE config review using pure NOOA agents

This module provides direct access to NOOA agents for analyzing FORGE test configurations
without compatibility wrappers. Users interact with agents directly for maximum performance.
"""

# Add project root to path when run directly (must be before other imports)
import sys
from pathlib import Path

if __name__ == "__main__":
    # Add the project root (5 levels up from this file) to Python path
    # projects/core/agentic/config_review/cli.py -> /home/kpouget/openshift/forge-censoring
    project_root = Path(__file__).parent.parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import json
import logging
from typing import Any

import click
import yaml
from projects.core.agentic.models import create_llm_client, load_model_config

from projects.core.library import vault

# Handle both relative and direct imports
try:
    from .agent import ConfigContext, ConfigReviewAgent
except ImportError:
    from projects.core.agentic.config_review.agent import ConfigContext, ConfigReviewAgent

logger = logging.getLogger(__name__)

DEFAULT_MODEL_KEY = "qwen-3-6-35b"


def parse_artifact_directory(artifact_dir: Path) -> dict[str, Any]:
    """
    Parse FORGE artifact directory to extract configuration data

    Args:
        artifact_dir: Path to FORGE artifact directory

    Returns:
        Dictionary containing parsed configuration data
    """
    artifact_data = {
        "config": {},
        "execution_engine": {},
        "presets_applied": "",
        "config_overrides": [],
        "project_name": "unknown",
        "available_files": [],
    }

    # Look for common FORGE configuration files
    config_files = [
        "config.yaml",
        "config.yml",
        "test-config.yaml",
        "config.json",
        "execution.yaml",
        "run.yaml",
    ]

    available_files = []
    for config_file in artifact_dir.rglob("*"):
        if config_file.is_file():
            available_files.append(str(config_file.relative_to(artifact_dir)))

    artifact_data["available_files"] = available_files

    # Try to find and parse configuration files
    for config_file in config_files:
        config_path = artifact_dir / config_file
        if config_path.exists():
            try:
                with open(config_path) as f:
                    if config_file.endswith((".yaml", ".yml")):
                        config_data = yaml.safe_load(f)
                    else:
                        config_data = json.load(f)

                    if config_data:
                        artifact_data["config"].update(config_data)
                        logger.debug(f"Loaded config from {config_file}")
                        break
            except Exception as e:
                logger.warning(f"Failed to parse {config_file}: {e}")

    # Extract execution engine info if available
    if "execution" in artifact_data["config"]:
        artifact_data["execution_engine"] = artifact_data["config"]["execution"]

    # Extract project name - handle both simple strings and nested dictionaries
    project_info = (
        artifact_data["config"].get("project")
        or artifact_data["execution_engine"].get("project")
        or artifact_dir.name
    )

    # If project is a dictionary, extract the name field
    if isinstance(project_info, dict):
        project_name = project_info.get("name", str(project_info))
    else:
        project_name = project_info or artifact_dir.name

    artifact_data["project_name"] = project_name

    # Extract config overrides
    overrides = artifact_data["execution_engine"].get("configOverrides", []) or artifact_data[
        "config"
    ].get("configOverrides", [])
    artifact_data["config_overrides"] = overrides if isinstance(overrides, list) else []

    return artifact_data


def format_analysis_output(analysis, artifact_dir: str, output_format: str) -> str:
    """
    Format NOOA agent analysis results for output

    Args:
        analysis: ConfigAnalysis object from NOOA agent
        artifact_dir: Artifact directory path
        output_format: Output format ("text" or "json")

    Returns:
        Formatted output string
    """
    if output_format == "json":
        return _format_json_output(analysis, artifact_dir)
    else:
        return _format_text_output(analysis, artifact_dir)


def _format_text_output(analysis, artifact_dir: str) -> str:
    """Format analysis results for text output"""
    output_lines = [
        f"🤖 NOOA Config Analysis Results for: {artifact_dir}",
        "=" * 60,
        "",
        "📊 Test Analysis Summary",
        "",
        f"🎯 Test Description: {analysis.test_description}",
        "",
        f"🔄 Changes Summary: {analysis.changes_summary}",
        "",
        f"📈 Testing Focus: {analysis.testing_focus}",
        "",
        f"⚙️ Configuration Context: {analysis.configuration_context}",
        "",
        "📋 Test Details:",
        f"  - Type: {analysis.test_type}",
        f"  - Scope: {analysis.estimated_scope}",
        f"  - Risk: {analysis.risk_assessment}",
        "",
    ]

    if analysis.key_parameters:
        output_lines.append("🔧 Key Parameters:")
        for param in analysis.key_parameters:
            output_lines.append(f"  - {param}")
        output_lines.append("")

    if analysis.expected_outcomes:
        output_lines.append("🎯 Expected Outcomes:")
        for outcome in analysis.expected_outcomes:
            output_lines.append(f"  - {outcome}")
        output_lines.append("")

    if analysis.configuration_issues:
        output_lines.append("⚠️ Configuration Issues:")
        output_lines.append(f"  {analysis.configuration_issues}")
        output_lines.append("")

    return "\n".join(output_lines)


def _format_json_output(analysis, artifact_dir: str) -> str:
    """Format analysis results for JSON output"""
    result = {
        "status": "success",
        "agent_type": "nooa",
        "artifact_dir": artifact_dir,
        "analysis": {
            "test_description": analysis.test_description,
            "changes_summary": analysis.changes_summary,
            "testing_focus": analysis.testing_focus,
            "configuration_context": analysis.configuration_context,
            "test_type": analysis.test_type,
            "estimated_scope": analysis.estimated_scope,
            "key_parameters": analysis.key_parameters,
            "risk_assessment": analysis.risk_assessment,
            "expected_outcomes": analysis.expected_outcomes,
            "configuration_issues": analysis.configuration_issues,
        },
    }

    return json.dumps(result, indent=2)


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
@click.option("--verbose", is_flag=True, help="Show verbose output during analysis")
@click.option("--model-key", default=DEFAULT_MODEL_KEY, help="Model key to use for analysis")
def cli(base_artifact_dir: Path, output_format: str, verbose: bool, model_key: str):
    """
    FORGE Config Review CLI - Direct NOOA agent analysis

    This tool uses NOOA agents directly to analyze FORGE test configurations.
    The agent provides structured analysis of what is being tested based on
    configuration changes and test parameters.

    Examples:
        # Basic config analysis
        forge config-review --base-artifact-dir /path/to/artifacts

        # JSON output for programmatic use
        forge config-review --base-artifact-dir /path/to/artifacts --output-format json

        # Verbose analysis with detailed progress
        forge config-review --base-artifact-dir /path/to/artifacts --verbose
    """
    # Set logging level - always show INFO for LLM calls
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
    else:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    try:
        logger.info(f"🤖 Starting NOOA config analysis for: {base_artifact_dir}")

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

        # Parse artifact directory
        if verbose:
            click.echo("Parsing artifact directory...")

        artifact_data = parse_artifact_directory(base_artifact_dir)

        # Create NOOA agent
        agent = ConfigReviewAgent(llm=llm)

        # Create context for the agent
        context = ConfigContext(
            artifact_dir=base_artifact_dir,
            config_data=artifact_data["config"],
            execution_engine=artifact_data["execution_engine"],
            presets_applied=artifact_data["presets_applied"],
            config_overrides=artifact_data["config_overrides"],
            project_name=artifact_data["project_name"],
            available_files=artifact_data["available_files"],
        )

        # Use NOOA agent to analyze the configuration
        if verbose:
            click.echo(f"Analyzing configuration for project: {context.project_name}")

        analysis = agent.analyze_config(context, verbose=verbose)

        # Format and display results
        output = format_analysis_output(analysis, str(base_artifact_dir), output_format)
        click.echo(output)

        logger.info("✅ Successfully completed configuration analysis")
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

        logger.error(f"❌ Config analysis failed: {e}")
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
            click.echo(f"❌ Config analysis failed: {e}")
            click.echo("\nFull traceback:")
            click.echo(traceback.format_exc())
        exit(1)


if __name__ == "__main__":
    cli()
