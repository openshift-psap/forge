#!/usr/bin/env python3
"""
FOURNOS launcher project CI Operations

"""

import logging
import types

import click

from projects.core.library import ci as ci_lib
from projects.core.library import config
from projects.fournos_launcher.orchestration import job_management, utils
from projects.fournos_launcher.orchestration import submit as submit_mod

logger = logging.getLogger(__name__)


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """FOURNOS Project launcher CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    submit_mod.init()
    utils.ensure_oc_available()

    # Set CI job label for tracking and cancellation
    ci_label = job_management.generate_ci_job_label()
    if ci_label:
        config.project.set_config("fournos.job.ci_label", ci_label)
        logger.info(f"Set CI job label: {ci_label}")


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def submit(ctx):
    """Submit a CI job to FOURNOS CI entrypoint."""
    return submit_mod.submit_job()


if __name__ == "__main__":
    main()
