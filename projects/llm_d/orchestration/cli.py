#!/usr/bin/env python3
"""
LLM-D Project CLI entrypoint
"""

import logging
import types
from pathlib import Path

import click

from projects.cluster.library.prom.metrics import capture_metrics_command
from projects.core.library import config, env, run
from projects.core.library.postprocess import postprocess_command

logger = logging.getLogger(__name__)


@click.group()
@click.option(
    "--preset",
    multiple=True,
    help="Apply a preset to the configuration. Pass multiple --preset NAME to apply multiple presets.",
)
@click.pass_context
def main(ctx, preset):
    """CLI Operations."""
    ctx.ensure_object(types.SimpleNamespace)

    env.init()
    run.init()

    if preset:
        config.write_variables_override(presets=preset)

    config.init(Path(__file__).parent)


@main.command()
@click.option(
    "--deployment-profile",
    multiple=True,
    help="The deployment profile(s) to deploy",
)
@click.option(
    "--benchmark-key",
    multiple=True,
    help="The benchmark key(s) to launch.",
)
@click.option(
    "--stop-on-error/--continue-on-error",
    default=True,
    help="Stop on the first test error (default: true)",
)
@click.pass_context
def deploy_and_test(ctx, deployment_profile, benchmark_key, stop_on_error) -> int:
    """Test LLM-D with specified deployment profiles and benchmark keys."""
    try:
        # Configure runtime settings based on provided options
        if deployment_profile:
            config.project.set_config("runtime.deployment_profile", list(deployment_profile))

        if benchmark_key:
            config.project.set_config("runtime.benchmark_key", list(benchmark_key))

        # Initialize vaults for deployment
        from projects.llm_d.orchestration.ci import init_vaults_for_phase
        from projects.llm_d.orchestration.test_phase import run_all_tests

        # Test execution
        logger.info("Starting test phase...")
        init_vaults_for_phase("test")

        max_exit_code = run_all_tests(stop_on_error=stop_on_error)

        if max_exit_code != 0:
            logger.error(f"Test phase completed with exit code {max_exit_code}")
            return max_exit_code

        logger.info("All tests completed successfully!")
        return 0

    except Exception:
        logger.exception("Deploy and test failed")
        return 1


main.add_command(capture_metrics_command)
main.add_command(postprocess_command)


if __name__ == "__main__":
    main()
