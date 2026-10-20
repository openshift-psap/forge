#!/usr/bin/env python3
"""
AG Praxis Project CLI entrypoint
"""

import logging
import sys
import types
from pathlib import Path

import click
import test_phase

from projects.core.library import config, env, run
from projects.core.library.cli import safe_cli_command
from projects.core.library.postprocess import postprocess_command

logger = logging.getLogger(__name__)


def init(presets=None):
    """Initialize AG Praxis orchestration environment"""
    env.init()
    run.init()

    config.init(Path(__file__).parent)

    try:
        for preset_name in presets:
            logger.info(f"Applying preset: {preset_name}")
            config.project.apply_preset(preset_name)
    except ValueError as e:
        logger.error(f"Failed to apply preset '{preset_name}': {e}")
        sys.exit(1)


@click.group()
@click.option("--presets", multiple=True, help="Apply presets to the configuration")
@click.pass_context
def main(ctx, presets):
    """CLI Operations."""
    ctx.ensure_object(types.SimpleNamespace)

    init(presets)


@main.command()
@click.pass_context
@safe_cli_command
def test(ctx):
    """Test phase - Execute the main testing logic."""
    exit_code = test_phase.test()
    sys.exit(exit_code)


@main.command()
@click.option(
    "--benchmark",
    type=click.Choice(["svc", "maas"]),
    default=None,
    help="Run benchmark against 'svc' (direct LLMInferenceService) or 'maas' (MaaS external gateway)",
)
@click.pass_context
@safe_cli_command
def deploy(ctx, benchmark):
    """Deploy the LLMInferenceService and optionally benchmark one endpoint."""
    config.project.set_config("test.skip_cleanup", True)
    config.project.set_config("caliper.postprocess.enabled", False)

    if benchmark:
        config.project.set_config("test.endpoints", [benchmark])
        config.project.set_config("test.run_benchmark", True)
    else:
        config.project.set_config("test.run_benchmark", False)

    exit_code = test_phase.test()
    sys.exit(exit_code)


main.add_command(postprocess_command)


if __name__ == "__main__":
    main()
