#!/usr/bin/env python3
"""
Skeleton Example Project CI Operations

This is a skeleton/template project that demonstrates how to create a new project
within the FORGE test harness framework. Use this as a starting point for
building your own projects.
"""

import logging
import pathlib
import types

import click
import prepare_skeleton
import test_skeleton

from projects.core.agentic.config_review import trigger_config_review_for_ci
from projects.core.agentic.on_failure import agent_review_on_failure
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault
from projects.core.library.export import (
    caliper_agentic_list_vaults,
    caliper_export_entrypoint,
    caliper_export_list_optional_vaults,
    caliper_export_list_vaults,
)
from projects.core.library.replot import caliper_replot_entrypoint

logger = logging.getLogger(__name__)


def init():
    env.init()
    run.init()
    config.init(pathlib.Path(__file__).parent)


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """Skeleton example project CI operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    init()

    if ctx.invoked_subcommand == "resolve-fournos-config":
        logger.info("No need to initialize the vaults for the resolve step")
        return

    vault.phase_vault_init(ctx.invoked_subcommand)


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def prepare(ctx):
    """Prepare phase - Set up environment and dependencies."""
    return prepare_skeleton.prepare()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def test(ctx):
    """Test phase - Execute the main testing logic."""

    # Trigger config review analysis
    trigger_config_review_for_ci(env.BASE_ARTIFACT_DIR)

    return test_skeleton.test()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def pre_cleanup(ctx):
    """Cleanup phase - Clean up resources and finalize."""
    return prepare_skeleton.cleanup()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def post_cleanup(ctx):
    """Cleanup phase - Clean up resources and finalize."""
    return prepare_skeleton.cleanup()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def preflight(ctx) -> int:
    """Preflight check phase - Validate that the cluster if ready for testing."""

    logger.warning("Nothing so far for the preflight check")

    return 0


main.add_command(caliper_export_entrypoint)
main.add_command(caliper_replot_entrypoint)

main.add_command(
    create_fournos_resolve_entrypoint(
        vault_list_funcs=[
            vault.phase_vault_list_all,
            caliper_export_list_vaults,
            caliper_export_list_optional_vaults,
            caliper_agentic_list_vaults,
        ],
        hardware_resolver_func=test_skeleton.resolve_hardware_request,
    )
)


if __name__ == "__main__":
    main()
