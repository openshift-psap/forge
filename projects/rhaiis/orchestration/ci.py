#!/usr/bin/env python3

import logging
import os
import types
from pathlib import Path

import click
import prepare_rhaiis
import test_rhaiis

from projects.core.agentic_review.config_review import trigger_config_review_for_ci
from projects.core.agentic_review.failure_review import agent_review_on_failure
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import env, vault
from projects.core.library.export import caliper_export_entrypoint
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


def _check_pipeline_failure_and_notify() -> None:
    """Detect early pipeline failures (e.g. image pull errors) and send a Slack alert.

    Runs in the post-cleanup finally step. Checks whether prior steps
    produced FAILURE artifacts or the test step was skipped entirely.
    """
    try:
        base_dir_env = os.environ.get("ARTIFACT_BASE_DIR", "")
        if not base_dir_env:
            return
        base_dir = Path(base_dir_env)
        if not base_dir.is_dir():
            return

        test_dir = base_dir / "03__test"
        test_ran = test_dir.exists()

        # Skip steps already handled by do_test's exception handler
        failure_files = sorted(f for f in base_dir.glob("*/FAILURE") if f.parent.name != "03__test")

        if not failure_files and test_ran:
            return

        errors = []
        for f in failure_files:
            step_name = f.parent.name
            content = f.read_text().strip()
            summary = content[:300] if content else "unknown error"
            errors.append(f"[{step_name}] {summary}")

        if not test_ran and not errors:
            errors.append(
                "Test step was skipped — likely an earlier pipeline step failed (e.g. image pull timeout)"
            )

        error_text = "\n".join(errors)

        from projects.core.library import config as _cfg
        from projects.rhaiis.postprocess.regression import send_failure_notification

        model_key = _cfg.project.get_config("tests.rhaiis.model_key", "unknown")
        try:
            model_cfg = runtime_config.get_model(model_key)
            model_name = model_cfg.get("hf_model_id", model_key)
        except Exception:
            model_name = model_key

        accelerator = runtime_config.get_accelerator()
        gpu_type = runtime_config.get_gpu_type(accelerator) or accelerator
        cluster_tag = _cfg.project.get_config("rhaiis.cluster_tag", "")
        accelerator_key = f"{gpu_type}_{cluster_tag}".upper() if cluster_tag else gpu_type.upper()

        send_failure_notification(
            error=error_text,
            model=model_name,
            accelerator=accelerator_key,
            job_id=os.environ.get("FJOB_NAME", ""),
            slack_user=_cfg.project.get_config("tests.rhaiis.slack_user", ""),
            notification_vault="psap-forge-notifications",
            version=_cfg.project.get_config("tests.rhaiis.version", ""),
            cluster=cluster_tag,
        )
    except Exception:
        logger.warning("Failed to check/send pipeline failure notification", exc_info=True)


def list_vaults() -> list[str]:
    test_rhaiis.init()
    return runtime_config.get_vaults()


def resolve_hardware_request(hardware_spec: dict) -> dict:
    test_rhaiis.init()

    if hardware_spec.get("gpuType"):
        return hardware_spec

    from projects.core.library import config as _cfg

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
    _check_pipeline_failure_and_notify()
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
