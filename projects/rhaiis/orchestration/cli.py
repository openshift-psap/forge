#!/usr/bin/env python3
"""RHAIIS CLI - Detailed CLI for interactive/manual use.

This CLI provides full control over benchmark parameters via command-line options.
For FOURNOS jobs that read config from files, use ci.py instead.

Examples:
    # Single workload
    cli.py test --model qwen-0.6b --workload balanced

    # Multiple workloads (deploy vLLM once)
    cli.py test --model qwen-0.6b --workloads balanced,short,heterogeneous

    # AMD accelerator
    cli.py test --model qwen-0.6b --accelerator amd
"""

import sys
from pathlib import Path

import click

from projects.core.workflow import WorkflowContext

from . import test_rhaiis

# Default config directory
DEFAULT_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config" / "rhaiis"


@click.group()
def cli():
    """RHAIIS CLI - Interactive benchmark commands."""


@cli.command()
@click.option(
    "--rhoai-version",
    envvar="FORGE_RHOAI_VERSION",
    default="2.19",
    help="RHOAI operator version to install",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be done without executing",
)
def prepare(rhoai_version: str, dry_run: bool):
    """Install operators (RHOAI, NFD, GPU) on OpenShift."""
    ctx = WorkflowContext.from_environment()
    ctx.write_metadata({"command": "prepare", "rhoai_version": rhoai_version})

    if dry_run:
        click.echo(f"[DRY-RUN] Would install RHOAI {rhoai_version}")
        click.echo(f"[DRY-RUN] Artifacts would be written to: {ctx.artifact_dir}")
        return

    exit_code = test_rhaiis.run_prepare(ctx, rhoai_version)
    sys.exit(exit_code)


@cli.command()
@click.option(
    "--model",
    envvar="FORGE_MODEL",
    default=None,
    help="Model key or HuggingFace ID (e.g., qwen-0.6b or Qwen/Qwen3-0.6B)",
)
@click.option(
    "--workload",
    envvar="FORGE_WORKLOAD",
    default=None,
    help="Single workload: balanced, heterogeneous, multiturn, etc.",
)
@click.option(
    "--workloads",
    envvar="FORGE_WORKLOADS",
    default=None,
    help="Comma-separated workloads to run WITHOUT restarting vLLM (e.g., balanced,short,heterogeneous)",
)
@click.option(
    "--config-dir",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Config directory containing defaults.yaml, models.yaml, workloads.yaml",
)
@click.option(
    "--accelerator",
    envvar="FORGE_ACCELERATOR",
    type=click.Choice(["nvidia", "amd"]),
    default="nvidia",
    help="Accelerator type for config inheritance (nvidia, amd)",
)
@click.option(
    "--vllm-image",
    envvar="FORGE_VLLM_IMAGE",
    help="vLLM container image to use (overrides accelerator default)",
)
@click.option(
    "--tensor-parallel",
    envvar="FORGE_TENSOR_PARALLEL",
    type=int,
    default=None,
    help="Tensor parallelism override (default: from model config)",
)
@click.option(
    "--max-requests",
    envvar="FORGE_MAX_REQUESTS",
    type=int,
    default=None,
    help="Maximum requests for GuideLLM benchmark (default: from config)",
)
@click.option(
    "--namespace",
    envvar="FORGE_NAMESPACE",
    default=None,
    help="Kubernetes namespace for deployment (default: from config)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be done without executing",
)
def test(
    model: str | None,
    workload: str | None,
    workloads: str | None,
    config_dir: Path | None,
    accelerator: str,
    vllm_image: str | None,
    tensor_parallel: int | None,
    max_requests: int | None,
    namespace: str | None,
    dry_run: bool,
):
    """Run benchmark: deploy vLLM -> run GuideLLM -> collect artifacts.

    \b
    Config inheritance (defaults -> accelerator -> model):
        defaults.yaml provides base settings
        accelerator (nvidia/amd) provides accelerator-specific overrides
        model config provides model-specific settings
        CLI flags override everything

    \b
    Modes of operation:
    1. Single workload:     --model X --workload balanced
    2. Multiple workloads:  --model X --workloads balanced,short,heterogeneous
       (deploys vLLM once, runs GuideLLM multiple times)
    """
    ctx = WorkflowContext.from_environment()
    config_dir = config_dir or DEFAULT_CONFIG_DIR

    # Parse workloads list
    workload_list = workloads.split(",") if workloads else None

    # Validate inputs (skip for dry-run)
    if not dry_run and not model:
        click.echo("Error: Must specify --model", err=True)
        sys.exit(1)

    exit_code = test_rhaiis.run_test(
        ctx=ctx,
        model=model,
        workload=workload,
        workloads=workload_list,
        config_dir=config_dir,
        accelerator=accelerator,
        vllm_image=vllm_image,
        tensor_parallel=tensor_parallel,
        max_requests=max_requests,
        namespace=namespace,
        dry_run=dry_run,
    )
    sys.exit(exit_code)


@cli.command()
@click.option(
    "--namespace",
    envvar="FORGE_NAMESPACE",
    default="forge",
    help="Kubernetes namespace to clean up",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be done without executing",
)
def cleanup(namespace: str, dry_run: bool):
    """Uninstall operators and cleanup resources."""
    ctx = WorkflowContext.from_environment()
    ctx.write_metadata({"command": "cleanup", "namespace": namespace})

    if dry_run:
        click.echo(f"[DRY-RUN] Would clean up namespace: {namespace}")
        return

    exit_code = test_rhaiis.run_cleanup(ctx, namespace)
    sys.exit(exit_code)


if __name__ == "__main__":
    cli()
