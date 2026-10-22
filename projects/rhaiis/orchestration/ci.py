#!/usr/bin/env python3

import logging
import types

import click
import prepare_rhaiis
import test_rhaiis

from projects.caliper.orchestration.export import ensure_mlflow_destination_marker
from projects.core.agentic.config_review import trigger_config_review_for_ci
from projects.core.agentic.on_failure import agent_review_on_failure
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config as _cfg
from projects.core.library import env, vault
from projects.core.library.ci import ensure_kubeconfig_works
from projects.core.library.export import caliper_export_entrypoint
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


def list_vaults() -> list[str]:
    test_rhaiis.init()
    return runtime_config.get_vaults()


def resolve_hardware_request(hardware_spec: dict) -> dict:
    test_rhaiis.init()

    if hardware_spec.get("gpuType"):
        return hardware_spec

    model_key = runtime_config.get_test_model_key()
    model = runtime_config.get_model(model_key)
    engine = runtime_config.get_engine()
    engine_defaults = _cfg.project.get_config(f"rhaiis.engines.{engine}.args") or {}
    ea = runtime_config.merge_engine_args(engine_defaults, model, {}, engine)
    tp_size = int(ea.get("tensor-parallel-size") or ea.get("tp-size") or ea.get("tp_size") or 1)

    accelerator = runtime_config.get_accelerator()
    gpu_type = runtime_config.get_gpu_type(accelerator)

    if not gpu_type:
        return {}

    hardware_spec["gpuCount"] = tp_size
    hardware_spec["gpuType"] = gpu_type

    return hardware_spec


@click.group()
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """RHAIIS Project CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    test_rhaiis.init()

    if ctx.invoked_subcommand == "resolve-fournos-config":
        return

    vault.init(runtime_config.get_vaults())
    ensure_mlflow_destination_marker()

    if ctx.invoked_subcommand == "export-artifacts":
        from projects.rhaiis.orchestration.slack_provider import RhaiisSlackProvider

        ctx.obj.notification_provider = RhaiisSlackProvider()
    else:
        ensure_kubeconfig_works()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def prepare(ctx):
    """Prepare phase - Set up environment and dependencies."""
    return prepare_rhaiis.prepare()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def test(ctx):
    """Test phase - Deploy model, run benchmarks, capture results."""
    trigger_config_review_for_ci(env.BASE_ARTIFACT_DIR, async_mode=True)
    return test_rhaiis.test()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def pre_cleanup(ctx):
    """Pre-cleanup phase - no-op to avoid cleaning up running resources."""
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def post_cleanup(ctx):
    """Post-cleanup phase - Clean up resources after test."""
    return prepare_rhaiis.cleanup()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def preflight(ctx) -> int:
    """Preflight check phase - Validate that the cluster if ready for testing."""

    logger.info("Nothing so far for the preflight check")

    return 0


main.add_command(caliper_export_entrypoint)
main.add_command(
    create_fournos_resolve_entrypoint(
        vault_list_func=list_vaults,
        hardware_resolver_func=resolve_hardware_request,
    )
)

if __name__ == "__main__":
    main()
