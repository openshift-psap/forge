"""Create an IBM Cloud cluster using release scripts and Forge DSL tasks."""

import json
import re
import shutil
import uuid
from pathlib import Path

import yaml

from projects.core.dsl import entrypoint, execute_tasks, task

from .. import common
from ..common import run_step_command, state_directory
from ..public import ClusterConfig, validate_vault_directory


@entrypoint
def run(*, state_dir: str, vault_dir: str, settings_file_path: str, job_name: str) -> dict:
    """Create a cluster, retaining installer assets in the internal workspace.

    Args:
        state_dir: Private persistent directory unique to this pipeline run.
        job_name: FournosJob name, or an explicit standalone run identifier.
        vault_dir: Read-only directory satisfying toolbox.public.CREATE_VAULT_FILES.
        settings_file_path: YAML matching toolbox.public.ClusterConfig (no outer key).
    """
    return execute_tasks(locals()).result


@task
def validate_inputs(args, ctx):
    """Validate cluster settings and the private workspace and vault paths."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.job_name):
        raise ValueError("job_name must be a lowercase Kubernetes job name")
    ctx.state_dir = state_directory(args.state_dir)
    ctx.settings = ClusterConfig.from_yaml(args.settings_file_path).model_dump()
    ctx.vault_dir = validate_vault_directory(args.vault_dir, for_create=True)
    return "Cluster inputs validated"


@task
def prepare_workspace(args, ctx):
    """Create private installation directories and a writable SSH key copy."""
    ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("shared", "logs", "install"):
        (ctx.state_dir / name).mkdir(mode=0o700)
    ctx.runtime = ctx.state_dir / "install"
    ssh = ctx.runtime / ".ssh"
    ssh.mkdir(mode=0o700)
    shutil.copyfile(ctx.vault_dir / "ssh-privatekey", ssh / "ssh-privatekey")
    (ssh / "ssh-privatekey").chmod(0o600)
    return "Private workspace prepared"


@task
def prepare_release_scripts(args, ctx):
    """Fetch the pinned release steps and save non-secret inputs for teardown."""
    common.prepare(ctx.settings["release"]["commit"], ctx.state_dir / "release")
    # Context is published by the DSL: store only paths and non-secret inputs.
    ctx.values = environment(ctx.settings, ctx.state_dir, ctx.vault_dir, args.job_name)
    (ctx.state_dir / "inputs.json").write_text(json.dumps(ctx.values))
    return "Release scripts and teardown inputs saved"


@task
def prepare_config_profile(args, ctx):
    """Normalize pull-secret JSON privately for the release YAML configuration step."""
    try:
        pull_secret = json.loads((ctx.vault_dir / "pull-secret").read_text())
    except json.JSONDecodeError:
        raise ValueError("Vault pull-secret must contain valid JSON") from None
    if not isinstance(pull_secret, dict):
        raise ValueError("Vault pull-secret must contain a JSON object")
    ctx.config_profile = ctx.state_dir / "config-profile"
    ctx.config_profile.mkdir(mode=0o700)
    normalized = ctx.config_profile / "pull-secret"
    normalized.write_text(json.dumps(pull_secret, separators=(",", ":")))
    normalized.chmod(0o600)
    shutil.copyfile(ctx.vault_dir / "ssh-publickey", ctx.config_profile / "ssh-publickey")
    return "Private configuration profile prepared; vault files remain unchanged"


@task
def configure_cluster(args, ctx):
    """Run release configuration steps and apply the requested IBM zones."""
    run_step_command(
        "conf",
        ctx.state_dir,
        ctx.runtime,
        {**ctx.values, "CLUSTER_PROFILE_DIR": str(ctx.config_profile)},
    )
    run_step_command("ibmcloud", ctx.state_dir, ctx.runtime, ctx.values)
    install_config_path = ctx.state_dir / "shared" / "install-config.yaml"
    try:
        install_config = yaml.safe_load(install_config_path.read_text())
    except yaml.YAMLError:
        raise ValueError("Invalid install-config; inspect the internal workspace") from None
    install_config["controlPlane"]["platform"]["ibmcloud"]["zones"] = ctx.settings["zones"]
    install_config["compute"][0]["platform"]["ibmcloud"]["zones"] = ctx.settings["zones"]
    # The upstream script emits this unquoted; YAML can coerce names such as "on".
    install_config["platform"]["ibmcloud"]["resourceGroupName"] = ctx.settings["resource_group"]
    install_config_path.write_text(yaml.safe_dump(install_config, sort_keys=False))
    return "Cluster installation configured"


@task
def configure_operator_credentials(args, ctx):
    """Run the release manual-credentials step inside private storage."""
    run_step_command("credentials", ctx.state_dir, ctx.runtime, ctx.values)
    return "Operator credential manifests prepared internally"


@task
def install_cluster(args, ctx):
    """Run the release installer with its asset directory on the PVC."""
    (ctx.state_dir / "install.started").touch()
    run_step_command("install", ctx.state_dir, ctx.runtime, ctx.values)
    ctx.result = {
        "status": "ready",
        "cluster_name": ctx.values["CLUSTER_NAME"],
        "job_name": ctx.values["JOB_NAME"],
    }
    return "Cluster installation completed"


def environment(settings: dict, root: Path, vault_dir: Path, job_name: str) -> dict:
    # Keep the generated name short; the full job name is retained in saved inputs.
    name_prefix = job_name.replace(".", "-")
    if len(name_prefix) > 14:
        name_prefix = f"{name_prefix[:8].rstrip('-')}-{name_prefix[-5:].lstrip('-')}"
    unique_hash = uuid.uuid4().hex[:12]
    values = common.defaults(root / "release")
    values.update(
        {
            "CLUSTER_TYPE": "ibmcloud",
            "CLUSTER_PROFILE_DIR": str(vault_dir),
            "SHARED_DIR": str(root / "shared"),
            "ARTIFACT_DIR": str(root / "logs"),
            "LEASE_PROXY_CLIENT_SH": str(root / "release" / "lease.sh"),
            "LEASED_RESOURCE": settings["region"],
            "NAMESPACE": name_prefix,
            "UNIQUE_HASH": unique_hash,
            "CLUSTER_NAME": f"{name_prefix}-{unique_hash}",
            "JOB_NAME": job_name,
            "BUILD_ID": "1",
            "BASE_DOMAIN": settings["base_domain"],
            "RESOURCE_GROUP": settings["resource_group"],
            "CONTROL_PLANE_REPLICAS": str(settings["control_plane"]["count"]),
            "CONTROL_PLANE_INSTANCE_TYPE": settings["control_plane"]["type"],
            "WORKERS": str(settings["workers"]["count"]),
            "COMPUTE_NODE_TYPE": settings["workers"]["type"],
            "SIZE_VARIANT": "compact" if settings["workers"]["count"] == 0 else "default",
            "ZONES_COUNT": str(len(settings["zones"])),
            "PUBLISH": "External",
            "RELEASE_IMAGE_LATEST": settings["release"]["image"],
            "OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE": settings["release"]["image"],
            "CUSTOM_OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE": settings["release"]["image"],
            "OPENSHIFT_INSTALL_PROMTAIL_ON_BOOTSTRAP": "false",
            "OPENSHIFT_INSTALL_GATHER_BOOTSTRAP": "false",
            "KUBECONFIG": "/dev/null",
        }
    )
    return values


if __name__ == "__main__":
    run.main()
