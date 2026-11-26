#!/usr/bin/env python3
"""Ephemeral IBM Cloud cluster lifecycle, without a workload test phase."""

import json
import os
from pathlib import Path

import click
import yaml

from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault
from projects.ibmcloud_cluster.toolbox.create.main import run as create_ibmcloud
from projects.ibmcloud_cluster.toolbox.destroy.main import run as destroy_ibmcloud
from projects.ibmcloud_cluster.toolbox.public import ClusterConfig, validate_vault_directory

IBM_CLOUD_VAULT = "ibmcloud-ipi"


def validate_spec(spec: dict) -> None:
    if (
        spec.get("pipeline") != "forge-ibmcloud-create-destroy"
        or spec.get("clusterless") is not True
    ):
        raise ValueError("Use pipeline forge-ibmcloud-create-destroy with clusterless: true")


def write_resource_group_artifact(state_dir: Path) -> None:
    """Save only the IBM resource-group name; keep metadata contents off stdout."""
    metadata_path = state_dir / "install" / "installer" / "metadata.json"
    if not metadata_path.is_file():
        return
    try:
        metadata = json.loads(metadata_path.read_text())
        resource_group = metadata["ibmcloud"]["resourceGroupName"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("Installer metadata has no valid IBM resource-group name") from None
    if not isinstance(resource_group, str) or not resource_group:
        raise ValueError("Installer metadata has no valid IBM resource-group name")
    artifact_path = env.ARTIFACT_DIR / "ibmcloud-resource-group.json"
    artifact_path.write_text(json.dumps({"resourceGroupName": resource_group}, indent=2) + "\n")


def lifecycle(action: str) -> int:
    if env.FORGE_SHARED_DIR is None:
        raise ValueError("The shared pipeline workspace is required")
    state = env.FORGE_SHARED_DIR / "ibmcloud"
    vault_dir = ""
    # Local cleanup after confirmed cloud deletion requires no credentials.
    if action == "create" or (
        (state / "install.started").exists() and not (state / "destroy.succeeded").exists()
    ):
        vault.init(mandatory_vaults=[IBM_CLOUD_VAULT])
        vault_dir = str(vault.get_vault_content_path(IBM_CLOUD_VAULT, "ibmcloud-api-key").parent)
    if action == "create":
        settings = ClusterConfig.model_validate(config.project.get_config("ibmcloud"))
        validate_vault_directory(vault_dir, for_create=True)
        settings_file = env.FORGE_SHARED_DIR / "ibmcloud-settings.yaml"
        settings_file.write_text(yaml.safe_dump(settings.model_dump()))
        try:
            result = create_ibmcloud(
                state_dir=str(state),
                vault_dir=vault_dir,
                settings_file_path=str(settings_file),
                job_name=os.environ["FJOB_NAME"],
            )
        finally:
            write_resource_group_artifact(state)
    else:
        try:
            write_resource_group_artifact(state)
        finally:
            # Recovery-artifact failures must never prevent cloud cleanup.
            try:
                if vault_dir:
                    validate_vault_directory(vault_dir, for_create=False)
                result = destroy_ibmcloud(state_dir=str(state), vault_dir=vault_dir)
            except Exception:
                ci_lib.add_notification_file(
                    "ibmcloud-cleanup-failed",
                    "IBM Cloud cleanup failed. Retain the job, PipelineRun, and private PVC "
                    "state until cleanup is completed. The resource group is recorded in "
                    "ibmcloud-resource-group.json when installer metadata is available.",
                )
                raise
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
