#!/usr/bin/env python3
"""RHAIIS CI Operations - Minimal CLI for FOURNOS jobs.

Most configuration comes from config/ directory via ConfigLoader.
FOURNOS only needs to set a few key parameters:
    FORGE_MODEL     - Model key to benchmark
    FORGE_WORKLOADS - Comma-separated workloads (optional)

For interactive use with detailed CLI options, use cli.py instead.
"""

import os
import sys
import types
from pathlib import Path

import click

from projects.core.workflow import WorkflowContext

from . import test_rhaiis

# Default config directory
DEFAULT_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config" / "rhaiis"


@click.group()
@click.pass_context
def ci(ctx):
    """RHAIIS CI Operations for FOURNOS."""
    ctx.ensure_object(types.SimpleNamespace)


@ci.command()
@click.option("--dry-run", is_flag=True, help="Print what would be done without executing")
@click.pass_context
def prepare(ctx, dry_run: bool):
    """Prepare phase - Install operators (RHOAI, NFD, GPU)."""
    rhoai_version = os.environ.get("FORGE_RHOAI_VERSION", "2.19")

    if dry_run:
        click.echo("[DRY-RUN] Prepare phase")
        click.echo(f"[DRY-RUN] RHOAI Version: {rhoai_version}")
        click.echo("[DRY-RUN] Would install: NFD, GPU Operator, RHOAI")
        return

    workflow_ctx = WorkflowContext.from_environment()
    workflow_ctx.write_metadata({
        "command": "prepare",
        "rhoai_version": rhoai_version,
    })

    exit_code = test_rhaiis.run_prepare(workflow_ctx, rhoai_version)
    sys.exit(exit_code)


@ci.command()
@click.option("--dry-run", is_flag=True, help="Print what would be done without executing")
@click.pass_context
def test(ctx, dry_run: bool):
    """Test phase - Run vLLM benchmark.

    Configuration from config/ directory. FOURNOS sets:
        FORGE_MODEL - Model key (e.g., qwen-0.6b)
        FORGE_WORKLOADS - Comma-separated workloads (optional)
    """
    workflow_ctx = WorkflowContext.from_environment()

    # Key parameters from FOURNOS
    model = os.environ.get("FORGE_MODEL")
    workloads_str = os.environ.get("FORGE_WORKLOADS")

    # Parse workloads
    workloads = None
    if workloads_str:
        workloads = [w.strip() for w in workloads_str.split(",")]

    # Log
    click.echo("RHAIIS CI Test")
    click.echo(f"  Model: {model}")
    if workloads:
        click.echo(f"  Workloads: {workloads}")

    # All other config comes from config/ directory via ConfigLoader
    exit_code = test_rhaiis.run_test(
        ctx=workflow_ctx,
        model=model,
        workloads=workloads,
        config_dir=DEFAULT_CONFIG_DIR,
        dry_run=dry_run,
    )
    sys.exit(exit_code)


@ci.command()
@click.option("--dry-run", is_flag=True, help="Print what would be done without executing")
@click.pass_context
def cleanup(ctx, dry_run: bool):
    """Cleanup phase - Remove deployments and resources."""
    namespace = os.environ.get("FORGE_NAMESPACE", "forge")

    if dry_run:
        click.echo("[DRY-RUN] Cleanup phase")
        click.echo(f"[DRY-RUN] Namespace: {namespace}")
        click.echo("[DRY-RUN] Would delete: InferenceServices, ServingRuntimes")
        return

    workflow_ctx = WorkflowContext.from_environment()
    workflow_ctx.write_metadata({
        "command": "cleanup",
        "namespace": namespace,
    })

    exit_code = test_rhaiis.run_cleanup(workflow_ctx, namespace)
    sys.exit(exit_code)


if __name__ == "__main__":
    ci()
