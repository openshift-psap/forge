#!/usr/bin/env python3
"""
Minimal Project CLI entrypoint
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
    """Initialize LLM-D orchestration environment"""
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


main.add_command(postprocess_command)


if __name__ == "__main__":
    main()
