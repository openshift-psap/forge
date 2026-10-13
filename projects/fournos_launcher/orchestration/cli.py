#!/usr/bin/env python3
"""
Fournos launcher project CLI Operations
"""

import logging
import shlex
import types

import click

from projects.core.library import config
from projects.core.library.cli import safe_cli_command
from projects.fournos_launcher.orchestration import job_management
from projects.fournos_launcher.orchestration import submit as submit_mod

logger = logging.getLogger(__name__)


@click.group()
@click.pass_context
def main(ctx):
    """FOURNOS Project launcher CI Operations for FORGE."""

    ctx.ensure_object(types.SimpleNamespace)
    submit_mod.init()


@main.command()
@click.option("--cluster", help="Target cluster name")
@click.option("--project", help="Project to run (e.g., llm_d)")
@click.option("--args", help="Arguments to pass to the project (space-separated string)")
@click.option("--namespace", help="Kubernetes namespace for the FOURNOS job (optional)")
@click.option("--override", "-o", multiple=True, help="Config overrides in key=value format")
@click.option("--commit", help="Git commit SHA to set as PULL_PULL_SHA (optional)")
@click.pass_context
@safe_cli_command
def submit(ctx, cluster, project, args, namespace, override, commit):
    """Submit a CI job to FOURNOS CI entrypoint."""

    # Parse args string into list
    args_list = []
    if args:
        args_list = shlex.split(args)

    # Parse overrides into dict
    extra_overrides = {}
    for override_str in override:
        if "=" in override_str:
            key, value = override_str.split("=", 1)
            extra_overrides[key] = value
        else:
            logger.warning(f"Ignoring invalid override format: {override_str} (expected key=value)")

    # Override config values if provided
    clusterless_mode = config.project.get_config("fournos.job.clusterless")

    if cluster:
        config.project.set_config("cluster.name", cluster)
    else:
        cluster = config.project.get_config("cluster.name")

        if not cluster and not clusterless_mode:
            raise ValueError(
                "--cluster or cluster.name is mandatory (unless clusterless mode is enabled)"
            )

    if cluster:
        logger.info(f"Using cluster {cluster}")
    else:
        logger.info("Using clusterless mode")

    if project:
        config.project.set_config("ci_job.project", project)
    else:
        project = config.project.get_config("ci_job.project")
        if not project:
            raise ValueError("--project or ci_job.project is mandatory")

    logger.info(f"Using project {project}")

    if args_list:
        config.project.set_config("ci_job.args", args_list)
        logger.info(f"Using args {args_list}")

    if namespace:
        config.project.set_config("fournos.namespace", namespace)
        logger.info(f"Using namespace {namespace}")

    if extra_overrides:
        config.project.set_config("extra_overrides", extra_overrides)
        logger.info(f"Using overrides {extra_overrides}")

    # Empty the fournos job environment variables
    config.project.set_config("fournos.job.env", {})

    # Set commit SHA if provided
    if commit:
        config.project.set_config("fournos.job.extra_env", {"PULL_PULL_SHA": commit})
        logger.info(f"Using commit SHA {commit}")

    return submit_mod.submit_job()


@main.command()
@click.pass_context
@safe_cli_command
def shutdown_jobs(ctx):
    """Shutdown any running FournosJobs for this CI run."""
    job_management.shutdown_fjobs_on_interrupt()
    return 0


if __name__ == "__main__":
    main()
