"""Ephemeral IBM Cloud cluster lifecycle, without a workload test phase."""

import json
import os
from pathlib import Path

import click

from projects.cluster.toolbox.ibmcloud.destroy import run as destroy_ibmcloud
from projects.cluster.toolbox.ibmcloud.main import run as create_ibmcloud
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault


def validate_spec(spec: dict) -> None:
    if (
        spec.get("pipeline") != "forge-ibmcloud-create-destroy"
        or spec.get("clusterless") is not True
    ):
        raise ValueError("Use pipeline forge-ibmcloud-create-destroy with clusterless: true")


def lifecycle(action: str) -> int:
    internal = os.environ.get("FORGE_INTERNAL_DIR")
    if not internal:
        raise ValueError("The internal pipeline workspace is required")
    state = Path(internal) / "ibmcloud"
    profile = ""
    # No credentials are needed when create never reached the installation step.
    if action == "create" or (state / "install.started").exists():
        vault.init(mandatory_vaults=["ibmcloud-ipi"])
        profile = str(vault.get_vault_content_path("ibmcloud-ipi", "ibmcloud-api-key").parent)
    if action == "create":
        result = create_ibmcloud(
            state_dir=str(state),
            profile_dir=profile,
            settings=config.project.get_config("ibmcloud"),
        )
    else:
        result = destroy_ibmcloud(state_dir=str(state), profile_dir=profile)
    (env.ARTIFACT_DIR / "ibmcloud-status.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    env.init()
    run.init()
    # Teardown uses the saved configuration, even if current overrides are invalid.
    if ctx.invoked_subcommand != "ibmcloud-destroy":
        config.init(Path(__file__).parent, apply_cluster_config=False)


@main.command("ibmcloud-create")
@click.pass_context
@ci_lib.safe_ci_entrypoint
def create(ctx):
    return lifecycle("create")


@main.command("ibmcloud-destroy")
@click.pass_context
@ci_lib.safe_ci_entrypoint
def destroy(ctx):
    return lifecycle("destroy")


main.add_command(
    create_fournos_resolve_entrypoint(
        vault_list_funcs=[vault.phase_vault_list_all],
        spec_resolver_func=validate_spec,
    )
)


if __name__ == "__main__":
    main()
