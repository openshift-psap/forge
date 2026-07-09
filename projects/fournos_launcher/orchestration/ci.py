#!/usr/bin/env python3
"""
FOURNOS launcher project CI Operations

"""

import json
import logging
import types

import click

from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.fournos_launcher.orchestration import job_management, utils
from projects.fournos_launcher.orchestration import submit as submit_mod

logger = logging.getLogger(__name__)


def _set_job_owner_from_pull_request():
    """
    Set job owner from pull request metadata if available

    Checks for pull_request.json file and extracts user.login to set as fournos.job.owner
    """
    pull_request_file = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "pull_request.json"

    # Guard: Check if file exists
    if not pull_request_file.exists():
        logger.debug("No pull request metadata found")
        return

    # Guard: Try to parse JSON
    try:
        with open(pull_request_file) as f:
            pr_data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to parse pull request metadata: {e}")
        return

    # Guard: Check if user.login exists
    user_login = pr_data.get("user", {}).get("login")
    if not user_login:
        logger.warning("No user.login found in pull request metadata")
        return

    # Set the job owner
    config.project.set_config("fournos.job.owner", user_login)
    logger.info(f"Set job owner from pull request: {user_login}")


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """FOURNOS Project launcher CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    submit_mod.init()
    utils.ensure_oc_available()

    # Set job owner from pull request metadata if available
    _set_job_owner_from_pull_request()

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
