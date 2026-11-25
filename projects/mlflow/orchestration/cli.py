#!/usr/bin/env python3
"""MLflow Tracing Performance Testing — CLI entrypoint"""

import logging
import types
from pathlib import Path

import click

from projects.core.library import config, env, run
from projects.core.library.postprocess import postprocess_command

logger = logging.getLogger(__name__)


def init(presets=None):
    """Initialize MLflow orchestration environment"""
    env.init()
    run.init()

    if presets:
        config.write_variables_override(presets=presets)

    config.init(Path(__file__).parent)


@click.group()
@click.option(
    "--preset",
    multiple=True,
    help="Apply a preset to the configuration. Pass multiple --preset NAME to apply multiple presets.",
)
@click.pass_context
def main(ctx, preset):
    """MLflow Tracing Performance Testing CLI."""
    ctx.ensure_object(types.SimpleNamespace)

    presets_list = list(preset)
    if presets_list and "," in presets_list[0]:
        lst = presets_list.pop(0)
        presets_list = lst.split(",") + presets_list

    init(presets_list or None)


@main.command()
@click.pass_context
def prepare(ctx) -> int:
    """Prepare phase - Enable MLflow operator, deploy infrastructure, load traces."""
    from projects.mlflow.orchestration.prepare_phase import run as prepare_run

    return prepare_run()


@main.command()
@click.pass_context
def test(ctx) -> int:
    """Test phase - Execute tracing performance tests."""
    from projects.mlflow.orchestration.test_phase import run as test_run

    return test_run()


@main.command()
@click.pass_context
def pre_cleanup(ctx) -> int:
    """Pre-cleanup - Remove per-test resources (MinIO, PostgreSQL, MLflow CR)."""
    from projects.mlflow.orchestration.cleanup_phase import run as cleanup_run

    return cleanup_run()


@main.command()
@click.pass_context
def post_cleanup(ctx) -> int:
    """Post-cleanup - Disable MLflow operator and delete namespace."""
    from projects.mlflow.orchestration.cleanup_phase import run_platform_cleanup

    return run_platform_cleanup()


main.add_command(postprocess_command)


if __name__ == "__main__":
    main()
