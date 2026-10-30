#!/usr/bin/env python3
"""Standalone Forge entrypoint for a run-scoped observability stack."""

from __future__ import annotations

from pathlib import Path

import click

from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault
from projects.core.library.ci import ensure_kubeconfig_works
from projects.observability.library.runtime import (
    claim_workload_namespace,
    ensure_observability,
    get_workload_namespace_owner,
    release_workload_namespace,
    resolve_run_id,
    teardown_observability,
    validate_observability_run,
)


def _settings() -> dict:
    return config.project.get_config("observability", {}, print=False, warn=False)


def _required_vaults() -> list[str]:
    settings = _settings()
    if not settings.get("enabled"):
        return []

    vault_name = settings.get("receiver", {}).get("objstore_vault", {}).get("name")
    if not vault_name:
        raise ValueError("observability.receiver.objstore_vault.name must be set")
    return [vault_name]


def _run_context() -> tuple[dict, str, str] | None:
    settings = _settings()
    if not settings.get("enabled", False):
        return None

    workload_namespace = settings.get("workload_namespace")
    if not workload_namespace:
        raise ValueError("observability.workload_namespace must be set")
    run_id = resolve_run_id(settings)
    validate_observability_run(settings, workload_namespace=workload_namespace, run_id=run_id)
    return settings, workload_namespace, run_id


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx) -> None:
    """Manage one Forge run's Thanos Receiver and monitoring configuration."""
    env.init()
    run.init()
    config.init(Path(__file__).parent)

    if ctx.invoked_subcommand == "resolve-fournos-config":
        return

    phase = ctx.invoked_subcommand
    vault.init(_required_vaults() if phase == "prepare" else [])
    if phase in {"prepare", "preflight"}:
        ensure_kubeconfig_works()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def prepare(ctx) -> int:
    """Start this run's Receiver and remote-write queues."""
    run_context = _run_context()
    if run_context is not None:
        settings, workload_namespace, run_id = run_context
        claim_workload_namespace(workload_namespace, run_id)
        ensure_observability(settings, workload_namespace=workload_namespace, run_id=run_id)
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def preflight(ctx) -> int:
    """Check cluster access before provisioning."""
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def test(ctx) -> int:
    """The observability project has no benchmark phase."""
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def pre_cleanup(ctx) -> int:
    """Leave ingestion active until the workload has stopped."""
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def post_cleanup(ctx) -> int:
    """Drain writes, upload the final block, and remove this run's Receiver."""
    run_context = _run_context()
    if run_context is not None:
        settings, workload_namespace, run_id = run_context
        owner = get_workload_namespace_owner(workload_namespace)
        if owner != run_id:
            raise RuntimeError(
                f"Namespace {workload_namespace} is not claimed by this "
                "observability run; refusing post-cleanup"
            )
        teardown_observability(settings, workload_namespace=workload_namespace, run_id=run_id)
        release_workload_namespace(workload_namespace, run_id)
    return 0


main.add_command(create_fournos_resolve_entrypoint(vault_list_func=_required_vaults))


if __name__ == "__main__":
    main()
