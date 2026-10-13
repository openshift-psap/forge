#!/usr/bin/env python3
"""
LLM-D Project CI Operations

"""

import logging
import signal
import types
from datetime import datetime
from pathlib import Path

import click

from projects.caliper.orchestration.export import ensure_mlflow_destination_marker
from projects.caliper.orchestration.postprocess_outcome import TestPhaseOutcome
from projects.core.agentic.config_review import trigger_config_review_for_ci
from projects.core.agentic.on_failure import agent_review_on_failure
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault
from projects.core.library.ci import ExitCategory, ensure_kubeconfig_works
from projects.core.library.export import (
    caliper_agentic_list_vaults,
    caliper_export_entrypoint,
    caliper_export_list_optional_vaults,
    caliper_export_list_vaults,
)
from projects.core.library.postprocess import run_postprocess_after_test
from projects.core.library.replot import caliper_replot_entrypoint
from projects.llm_d.orchestration.cleanup_phase import (
    cleanup_operators,
)
from projects.llm_d.orchestration.cleanup_phase import (
    run as cleanup_toolbox_run,
)
from projects.llm_d.orchestration.preflight_phase import run as preflight_toolbox_run
from projects.llm_d.orchestration.prepare_sequence import run_prepare_sequence
from projects.rhoai.library.deploy import list_mandatory_vaults as rhoai_list_mandatory_vaults

logger = logging.getLogger(__name__)


def init(presets=None):
    """Initialize LLM-D orchestration environment"""
    env.init()
    run.init()

    run.register_signal_callback(_signal_callback)

    # Set presets configuration if provided
    if presets:
        config.write_variables_override(presets=presets)

    config.init(Path(__file__).parent)


def init_vaults_for_phase(phase: str):
    """Initialize vaults for a specific CI phase, including conditional vaults."""

    extra_mandatory = []
    if phase == "prepare":
        extra_mandatory.extend(rhoai_list_mandatory_vaults())

    vault.phase_vault_init(phase, extra_mandatory=(extra_mandatory or None))


def _signal_callback(sig, frame, log_file):
    env.reset_artifact_dir()

    sig_name = signal.Signals(sig).name
    logger.info(f"Signal callback: received {sig_name}")
    if not log_file:
        return

    module_name = (
        Path(__file__).relative_to(env.FORGE_HOME).with_suffix("").as_posix().replace("/", ".")
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
    """LLM-D Project CI Operations for FORGE."""
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

    if ctx.invoked_subcommand != "export-artifacts":
        ensure_kubeconfig_works()


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

    # Run all tests first
    from projects.llm_d.orchestration.test_phase import run_all_tests

    max_exit_code = run_all_tests(stop_on_error=False)

    test_failed = max_exit_code != 0
    failure_message = f"Tests completed with exit code {max_exit_code}" if test_failed else None

    exit_msg = None
    # Run post-processing once after all tests
    try:
        if test_failed and failure_message:
            outcome = TestPhaseOutcome("FAILED", failure_message)
        elif max_exit_code == 0:
            outcome = TestPhaseOutcome("SUCCESS")
        else:
            outcome = TestPhaseOutcome("FAILED", f"exit_code={max_exit_code}")

        status = run_postprocess_after_test(env.BASE_ARTIFACT_DIR, test_outcome=outcome)

        if status is None or not status.get("success", False):
            if max_exit_code == 0:
                max_exit_code = 1  # Set exit code to 1 if post-processing failed but tests passed

    except Exception as e:
        exit_msg = f"Test failed: {e}"
        logger.exception(e)
        if max_exit_code == 0:
            max_exit_code = 1  # Set exit code to 1 if post-processing failed but tests passed

    if not max_exit_code:
        return 0, ExitCategory.SUCCESS, ""

    return (
        max_exit_code,
        ExitCategory.TEST_FAILURE,
        exit_msg or f"Tests failed with exit code {max_exit_code}",
    )


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def pre_cleanup(ctx) -> int:
    """Cleanup phase - Clean up resources and finalize."""
    from projects.llm_d.orchestration import runtime_config

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            cleanup_toolbox_run(namespace=run_spec.namespace)
    cleanup_operators()
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
            cleanup_toolbox_run(namespace=run_spec.namespace)
    cleanup_operators()
    return 0


def _resolve_spec_defaults(spec: dict) -> None:
    if not spec.get("cluster") and not spec.get("clusterless"):
        default_cluster = config.project.get_config("ci_job.llm_d.default_cluster", None)
        if default_cluster:
            spec["cluster"] = default_cluster
            logger.info(f"Set spec.cluster to default: {default_cluster}")


main.add_command(
    create_fournos_resolve_entrypoint(
        vault_list_funcs=[
            vault.phase_vault_list_all,
            rhoai_list_mandatory_vaults,
            vault.phase_vault_list_all,
            caliper_export_list_vaults,
            caliper_export_list_optional_vaults,
            caliper_agentic_list_vaults,
        ],
        spec_resolver_func=_resolve_spec_defaults,
    )
)
main.add_command(caliper_export_entrypoint)
main.add_command(caliper_replot_entrypoint)

if __name__ == "__main__":
    main()
