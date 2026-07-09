#!/usr/bin/env python3

import logging
import types

import click
import prepare_rhaiis
import test_rhaiis

from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import vault
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
    vllm_args = model.get("vllm_args", {})
    tp_size = int(vllm_args.get("tensor-parallel-size", 1))

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

    if ctx.invoked_subcommand != "resolve-fournos-config":
        vault.init(runtime_config.get_vaults())


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def prepare(ctx):
    """Prepare phase - Set up environment and dependencies."""
    return prepare_rhaiis.prepare()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def test(ctx):
    """Test phase - Deploy model, run benchmarks, capture results."""
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

    logger.warning("Nothing so far for the preflight check")

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
