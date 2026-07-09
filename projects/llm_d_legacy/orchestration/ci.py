#!/usr/bin/env python3
"""
Jump CI Project CI Operations

This is the JumpCI CI entrypoint. It's used to run TOPSAIL-ng remotely inside a VPN cluster"""

import sys
import traceback
from pathlib import Path
import logging

import click

from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import env
from projects.core.library import config as forge_config
from projects.core.library import ci as ci_lib
from projects.core.library.export import caliper_export_entrypoint
from projects.core.library.replot import caliper_replot_entrypoint
from projects.legacy.library import config
from projects.legacy.library import env as legacy_env
from projects.caliper.orchestration.export import run_from_orchestration_config
from projects.core.library import vault

logger = logging.getLogger(__name__)

# Add the testing directory to path for imports
testing_dir = Path(__file__).parent.parent / "testing"
if str(testing_dir) not in sys.path:
    sys.path.insert(0, str(testing_dir))

# Import llm_d legacy testing functionality
try:
    import prepare_llmd, test_llmd
    import test as test_mod

except ImportError as e:
    raise RuntimeError(f"Legacy LLM_D testing functionality not available: {e}") from e


def log(message: str, level: str = "info"):
    """Log message with project prefix."""
    project_name = "llm_d"
    icon = {"info": "ℹ️", "success": "✅", "error": "❌", "warning": "⚠️"}.get(level, "ℹ️")
    click.echo(f"{icon} [{project_name}] {message}")


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """Jump CI Project CI Operations for TOPSAIL-NG."""
    ctx.ensure_object(dict)
    init()

inited = False
def init():
    global inited
    if inited:
        return
    inited = True

    env.init()
    legacy_env.init()

    testing_dir = Path(__file__).parent.parent / "testing"
    config.init(testing_dir)
    forge_config.init(testing_dir)
    presets = config.project.get_config("project.args")
    for preset in presets:
        config.project.apply_preset(preset)

    test_mod.init()
    vault.disable_strict_validation()
    vault.init(config.project.get_config("vaults"))


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def test(ctx):
    """Test phase - Trigger the project's test method."""
    log("Starting test phase...")

    failed = test_llmd.test()

    sys.exit(1 if failed else 0)


main.add_command(caliper_export_entrypoint)
main.add_command(caliper_replot_entrypoint)

def resolve_hardware_request(hardware_spec: dict):
    """
    Resolve hardware requirements for FournosJob based on skeleton project configuration.

    This is a stub implementation. Update spec.hardware based on project configuration.

    Args:
        hardware_spec: The current spec.hardware dict from the FournosJob. This object should be updated.

    """
    init()

    logger.info("Hardware resolution: stub implementation - no changes made")

    # Stub implementation - could be extended to:
    # - Read hardware config from project config
    # - Set hardware requirements based on workload needs
    # - Handle different hardware profiles (GPU, CPU, memory requirements)
    # - Example: return {"gpu": {"type": "nvidia-tesla-v100", "count": 1}, "memory": "32Gi"}

    return hardware_spec

def list_vaults():
    init()
    return config.project.get_config("vaults")

main.add_command(
    create_fournos_resolve_entrypoint(
        vault_list_func=list_vaults,
        hardware_resolver_func=resolve_hardware_request,
    )
)


if __name__ == "__main__":
    main()
