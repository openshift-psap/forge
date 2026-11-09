#!/usr/bin/env python3

"""
CLI interface for FORGE failure analysis

Contains the Click CLI command and all related output formatting functions.
"""

import json
import logging
from pathlib import Path

import click

logger = logging.getLogger(__name__)

DEFAULT_MODEL_KEY = "qwen-3-6-35b"
DEFAULT_VAULT_NAME = "psap-models-corp-rh"
DEFAULT_VAULT_CONTENT = "agent-models.yaml"


def _resolve_vault_settings(model_key, vault_name, vault_content):
    """
    Fill in unset vault/model options from the project configuration

    Values not given on the command line are read from the
    ``agentic_review.*`` project configuration when it is initialized,
    and fall back to the defaults otherwise.
    """
    from projects.core.library import config

    if config.project is not None:
        if model_key is None:
            model_key = config.project.get_config(
                "agentic_review.model_key", None, print=False, warn=False
            )
        if vault_name is None:
            vault_name = config.project.get_config(
                "agentic_review.vault_name", None, print=False, warn=False
            )
        if vault_content is None:
            vault_content = config.project.get_config(
                "agentic_review.vault_content", None, print=False, warn=False
            )

    return (
        model_key or DEFAULT_MODEL_KEY,
        vault_name or DEFAULT_VAULT_NAME,
        vault_content or DEFAULT_VAULT_CONTENT,
    )


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
@click.option("--verbose", is_flag=True, help="Show all AI queries and responses")
@click.option("--model-key", default=None, help="Model key to use for analysis")
@click.option("--vault-name", default=None, help="Vault holding the model configuration")
@click.option(
    "--vault-content", default=None, help="Vault content file for the model configuration"
)
def cli(
    base_artifact_dir: Path,
    output_format: str,
    verbose: bool,
    model_key: str,
    vault_name: str,
    vault_content: str,
):
    """
    Failure Review Agent CLI - Analyze failure artifacts using LangChain LLM

    This tool processes FAILURE files and execution logs in the specified directory,
    provides AI-powered analysis of the failures, and supports DSL toolbox
    logs (task.log).
    """
    # Import functions from main module (only works when run from forge home directory)
    from projects.core.agentic_review.failure_review import run_on_failure_agent
    from projects.core.library import vault

    model_key, vault_name, vault_content = _resolve_vault_settings(
        model_key, vault_name, vault_content
    )

    try:
        # Initialize vault manager for CLI usage
        logger.info("Initializing vault manager...")
        vault.init(vaults=[vault_name])

        result = run_on_failure_agent(
            base_artifact_dir, verbose, model_key, vault_name, vault_content
        )

        if output_format == "json":
            click.echo(json.dumps(result, indent=2))
            return

        _print_header()

        if result.get("status") != "success":
            click.echo(f"❌ Error: {result.get('error')}")
            return

        _print_summary(result)

        if result.get("failures_found", 0) == 0:
            _print_no_failures_result(result)
            return

        _print_analysis_results(result)

    finally:
        if output_format != "json":
            click.echo("=" * 60)


def _print_header():
    """Print the results header"""
    click.echo("\n" + "=" * 60)
    click.echo("🎉 FAILURE REVIEW AGENT RESULTS:")
    click.echo("=" * 60)


def _print_summary(result):
    """Print summary information"""
    click.echo(f"📁 Artifact Directory: {result.get('artifact_dir')}")
    click.echo(f"🤖 Model: {result.get('model_used', 'unknown')}")
    click.echo(f"📊 Failures Found: {result.get('failures_found', 0)}")


def _print_no_failures_result(result):
    """Print result when no failures found"""
    click.echo("\n📝 Result:")
    click.echo("-" * 40)
    click.echo(result.get("analysis"))


def _print_analysis_results(result):
    """Print detailed analysis results"""
    click.echo(f"✅ Successful Analyses: {result.get('successful_analyses', 0)}")
    click.echo(f"❌ Failed Analyses: {result.get('failed_analyses', 0)}")

    analyses = result.get("analyses", [])
    for i, analysis in enumerate(analyses, 1):
        _print_single_analysis(analysis, i, len(analyses))


def _print_single_analysis(analysis, index, total):
    """Print a single failure analysis"""
    click.echo(f"\n🔍 Failure #{index}: {analysis.get('failure_dir')}")

    if analysis.get("investigated_files"):
        click.echo(f"📂 Investigated files: {', '.join(analysis['investigated_files'])}")

    if analysis.get("html_report"):
        click.echo(f"📄 HTML report: {analysis['html_report']}")

    if analysis.get("failure_review_file"):
        click.echo(f"📝 FAILURE_REVIEW: {analysis['failure_review_file']}")

    click.echo("-" * 60)

    if analysis.get("status") != "success":
        click.echo(f"❌ Error analyzing this failure: {analysis.get('error')}")
    else:
        _print_structured_analysis(analysis.get("structured_analysis"))
        click.echo(analysis.get("analysis"))

    if index < total:
        click.echo("\n" + "=" * 60)


def _print_structured_analysis(structured):
    """Print structured analysis if available"""
    if not structured:
        return

    if not any(structured.get(k) for k in ["root_cause", "failed_step", "trigger"]):
        return

    click.echo("\n📋 STRUCTURED ANALYSIS:")
    click.echo("-" * 30)

    if structured.get("root_cause"):
        click.echo(f"🔍 Root Cause: {structured['root_cause']}")
    if structured.get("failed_step"):
        click.echo(f"❌ Failed Step: {structured['failed_step']}")
    if structured.get("trigger"):
        click.echo(f"⚡ Trigger: {structured['trigger']}")

    click.echo("\n📝 FULL ANALYSIS:")
    click.echo("-" * 30)


if __name__ == "__main__":
    cli()
