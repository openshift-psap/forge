#!/usr/bin/env python3
"""
AG Praxis Forge Project CI Operations
"""

import logging
import pathlib
import signal
import types
from datetime import datetime

import click
import prepare_phase
import test_phase

from projects.caliper.orchestration.export import ensure_mlflow_destination_marker
from projects.core.agentic.config_review import trigger_config_review_for_ci
from projects.core.agentic.on_failure import agent_review_on_failure
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault

# from projects.core.library.ci import ensure_kubeconfig_works
from projects.core.library.export import (
    caliper_agentic_list_vaults,
    caliper_export_entrypoint,
    caliper_export_list_optional_vaults,
    caliper_export_list_vaults,
)
from projects.core.library.replot import caliper_replot_entrypoint

logger = logging.getLogger(__name__)


def init(presets=None):
    env.init()
    run.init()
    run.register_signal_callback(_signal_callback)

    if presets:
        config.write_variables_override(presets=presets)

    config.init(pathlib.Path(__file__).parent)


def init_vaults_for_phase(phase: str):
    """Initialize vaults for a specific CI phase."""

    vault.phase_vault_init(phase)


def _signal_callback(sig, frame, log_file):
    env.reset_artifact_dir()

    sig_name = signal.Signals(sig).name
    logger.info(f"Signal callback: received {sig_name}")
    if not log_file:
        return

    module_name = (
        pathlib.Path(__file__)
        .relative_to(env.FORGE_HOME)
        .with_suffix("")
        .as_posix()
        .replace("/", ".")
    )
    with log_file.open("a") as f:
        f.write(
            f"{datetime.now()}: {module_name}.{_signal_callback.__qualname__} {sig_name} handler\n"
        )


@click.group(cls=ci_lib.HelpfulGroup)
@click.option("--preset", multiple=True, help="Set preset configuration before starting")
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx, preset):
    """AG Praxis testing project CI operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)

    presets_list = list(preset)
    if presets_list and "," in presets_list[0]:
        lst = presets_list.pop(0)
        presets_list = lst.split(",") + presets_list

        logger.info(f"Setting preset configuration from CLI: {presets_list}")

    init(presets_list)

    if ctx.invoked_subcommand == "resolve-fournos-config":
        logger.info("No need to initialize the vaults for the resolve step")
        return

    init_vaults_for_phase(ctx.invoked_subcommand)
    ensure_mlflow_destination_marker()

    # if ctx.invoked_subcommand != "export-artifacts":
    #    ensure_kubeconfig_works()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def pre_cleanup(ctx):
    """Cleanup phase - Clean up resources and finalize."""
    return prepare_phase.cleanup()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def prepare(ctx):
    """Prepare phase - Set up the cluster ecosystem."""
    return prepare_phase.prepare()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def preflight(ctx) -> int:
    """Preflight check phase - Validate that the cluster if ready for testing."""

    logger.info("Nothing so far for the preflight check")

    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def test(ctx):
    """Test phase - Execute the main testing logic."""

    # Trigger config review analysis
    trigger_config_review_for_ci(env.BASE_ARTIFACT_DIR)

    return test_phase.test()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def post_cleanup(ctx):
    """Cleanup phase - Clean up the cluster resources and ecosystem."""
    return prepare_phase.cleanup()


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
        hardware_resolver_func=test_phase.fournos_resolve_hardware_request,
    )
)


if __name__ == "__main__":
    main()
