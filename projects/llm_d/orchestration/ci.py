#!/usr/bin/env python3
"""
LLM-D Project CI Operations

"""

import logging
import types
from pathlib import Path

import click

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
from projects.llm_d.orchestration.cleanup_phase import run as cleanup_toolbox_run
from projects.llm_d.orchestration.preflight_phase import run as preflight_toolbox_run
from projects.llm_d.orchestration.prepare_sequence import run_prepare_sequence
from projects.llm_d.orchestration.test_phase import run as test_toolbox_run

logger = logging.getLogger(__name__)


def init():
    """Initialize LLM-D orchestration environment"""
    env.init()
    run.init()
    config.init(Path(__file__).parent)


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """LLM-D Project CI Operations for FORGE."""
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
def prepare(ctx) -> int:
    """Prepare phase - Set up environment and dependencies."""
    return run_prepare_sequence()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def preflight(ctx) -> int:
    """Preflight check phase - Validate required CRDs exist before testing."""
    return preflight_toolbox_run()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def test(ctx) -> int:
    """Test phase - Execute the main testing logic."""
    # Trigger config review analysis asynchronously (don't block test execution)
    trigger_config_review_for_ci(env.BASE_ARTIFACT_DIR, async_mode=True)

    return test_toolbox_run()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def pre_cleanup(ctx) -> int:
    """Cleanup phase - Clean up resources and finalize."""
    from projects.llm_d.orchestration import runtime_config

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            cleanup_toolbox_run(namespace=run_spec.namespace, cleanup_subscriptions=True)
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def post_cleanup(ctx) -> int:
    """Cleanup phase - Clean up resources and finalize."""
    from projects.llm_d.orchestration import runtime_config

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            cleanup_toolbox_run(namespace=run_spec.namespace, cleanup_subscriptions=True)
    return 0


main.add_command(
    create_fournos_resolve_entrypoint(
        vault_list_funcs=[
            vault.phase_vault_list_all,
            caliper_export_list_vaults,
            caliper_export_list_optional_vaults,
            caliper_agentic_list_vaults,
        ]
    )
)
main.add_command(caliper_export_entrypoint)
main.add_command(caliper_replot_entrypoint)

if __name__ == "__main__":
    main()
