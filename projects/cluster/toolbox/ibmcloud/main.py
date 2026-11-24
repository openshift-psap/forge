"""Create an IBM Cloud cluster using release scripts and Forge DSL tasks."""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import yaml

from projects.core.dsl import entrypoint, execute_tasks, task

from . import upstream


@entrypoint
def run(*, state_dir: str, profile_dir: str, settings: dict) -> dict:
    """Create a cluster, retaining installer assets in the internal workspace.

    Args:
        state_dir: Private persistent directory unique to this pipeline run.
        profile_dir: Read-only ibmcloud-ipi vault directory, resolved by Forge.
        settings: Non-secret IBM settings from the project's configuration.
    """
    return execute_tasks(locals()).result


@task
def validate_inputs(args, ctx):
    """Validate cluster settings and the private workspace and vault paths."""
    ctx.root = state_directory(args.state_dir)
    ctx.profile = profile_directory(args.profile_dir)
    validate(args.settings)
    return "Cluster inputs validated"


@task
def prepare_workspace(args, ctx):
    """Create private installation directories and a writable SSH key copy."""
    ctx.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("shared", "logs", "install"):
        (ctx.root / name).mkdir(mode=0o700)
    ctx.runtime = ctx.root / "install"
    ssh = ctx.runtime / ".ssh"
    ssh.mkdir(mode=0o700)
    shutil.copyfile(ctx.profile / "ssh-privatekey", ssh / "ssh-privatekey")
    (ssh / "ssh-privatekey").chmod(0o600)
    return "Private workspace prepared"


@task
def prepare_release_scripts(args, ctx):
    """Fetch the pinned release steps and save non-secret inputs for teardown."""
    upstream.prepare(args.settings["release_commit"], ctx.root / "release")
    # Context is published by the DSL: store only paths and non-secret inputs.
    ctx.values = environment(args.settings, ctx.root, ctx.profile)
    (ctx.root / "inputs.json").write_text(json.dumps(ctx.values))
    return "Release scripts and teardown inputs saved"


@task
def prepare_config_profile(args, ctx):
    """Normalize pull-secret JSON privately for the release YAML configuration step."""
    try:
        pull_secret = json.loads((ctx.profile / "pull-secret").read_text())
    except json.JSONDecodeError:
        raise ValueError("Vault pull-secret must contain valid JSON") from None
    if not isinstance(pull_secret, dict):
        raise ValueError("Vault pull-secret must contain a JSON object")
    ctx.config_profile = ctx.root / "config-profile"
    ctx.config_profile.mkdir(mode=0o700)
    normalized = ctx.config_profile / "pull-secret"
    normalized.write_text(json.dumps(pull_secret, separators=(",", ":")))
    normalized.chmod(0o600)
    shutil.copyfile(ctx.profile / "ssh-publickey", ctx.config_profile / "ssh-publickey")
    return "Private configuration profile prepared; vault files remain unchanged"


@task
def configure_cluster(args, ctx):
    """Run release configuration steps and apply the requested IBM zones."""
    execute(
        "conf",
        ctx.root,
        ctx.runtime,
        {**ctx.values, "CLUSTER_PROFILE_DIR": str(ctx.config_profile)},
    )
    execute("ibmcloud", ctx.root, ctx.runtime, ctx.values)
    path = ctx.root / "shared" / "install-config.yaml"
    try:
        install_config = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        raise ValueError("Invalid install-config; inspect the internal workspace") from None
    install_config["controlPlane"]["platform"]["ibmcloud"]["zones"] = args.settings["zones"]
    install_config["compute"][0]["platform"]["ibmcloud"]["zones"] = args.settings["zones"]
    # The upstream script emits this unquoted; YAML can coerce names such as "on".
    install_config["platform"]["ibmcloud"]["resourceGroupName"] = args.settings["resource_group"]
    path.write_text(yaml.safe_dump(install_config, sort_keys=False))
    return "Cluster installation configured"


@task
def configure_operator_credentials(args, ctx):
    """Run the release manual-credentials step inside private storage."""
    execute("credentials", ctx.root, ctx.runtime, ctx.values)
    return "Operator credential manifests prepared internally"


@task
def install_cluster(args, ctx):
    """Run the release installer with its asset directory on the PVC."""
    (ctx.root / "install.started").touch()
    execute("install", ctx.root, ctx.runtime, ctx.values)
    ctx.result = {"status": "ready", "cluster_name": f"forge-{ctx.values['UNIQUE_HASH']}"}
    return "Cluster installation completed"


def state_directory(state_dir: str) -> Path:
    """Validate a shell-safe internal path outside the public artifact trees."""
    root = Path(state_dir).resolve()
    if not state_dir or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root)):
        raise ValueError("Internal workspace path must be shell-safe")
    for key in ("ARTIFACT_DIR", "ARTIFACT_BASE_DIR", "FORGE_BASE_ARTIFACT_DIR"):
        if os.environ.get(key):
            public = Path(os.environ[key]).resolve()
            if root == public or root in public.parents or public in root.parents:
                raise ValueError("Installer state must be outside the artifact workspace")
    return root


def profile_directory(profile_dir: str) -> Path:
    profile = Path(profile_dir).resolve()
    if not profile_dir or not (profile / "ibmcloud-api-key").is_file():
        raise ValueError("The ibmcloud-ipi vault directory is required")
    return profile


def validate(settings: dict) -> None:
    for key in ("base_domain", "resource_group", "release_image"):
        if not settings.get(key):
            raise ValueError(f"ibmcloud.{key} is required")
    if any(
        not isinstance(value, str) or any(c in value for c in "\n\r\0")
        for value in (settings["base_domain"], settings["resource_group"])
    ):
        raise ValueError("Domain and resource group must be single-line strings")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", settings["base_domain"]):
        raise ValueError("ibmcloud.base_domain must be a DNS domain")
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", settings["release_image"]):
        raise ValueError("ibmcloud.release_image must be pinned by digest")
    if not re.fullmatch(r"[0-9a-f]{40}", settings["release_commit"]):
        raise ValueError("ibmcloud.release_commit must be a full commit SHA")
    for key in ("region", "master_type", "worker_type"):
        if not isinstance(settings[key], str) or not re.fullmatch(
            r"[a-z][a-z0-9-]+", settings[key]
        ):
            raise ValueError(f"ibmcloud.{key} must be an IBM region or machine profile name")
    zones = settings["zones"]
    if (
        not isinstance(zones, list)
        or not zones
        or any(zone not in [f"{settings['region']}-{n}" for n in (1, 2, 3)] for zone in zones)
    ):
        raise ValueError("ibmcloud.zones must belong to the selected region")
    for key in ("masters", "workers"):
        if type(settings[key]) is not int or settings[key] < (1 if key == "masters" else 0):
            raise ValueError(f"ibmcloud.{key} has an invalid replica count")


def environment(settings: dict, root: Path, profile: Path) -> dict:
    values = upstream.defaults(root / "release")
    values.update(
        {
            "CLUSTER_TYPE": "ibmcloud",
            "CLUSTER_PROFILE_DIR": str(profile),
            "SHARED_DIR": str(root / "shared"),
            "ARTIFACT_DIR": str(root / "logs"),
            "LEASE_PROXY_CLIENT_SH": str(root / "release" / "lease.sh"),
            "LEASED_RESOURCE": settings["region"],
            "NAMESPACE": "forge",
            "UNIQUE_HASH": uuid.uuid4().hex[:12],
            "JOB_NAME": "forge-ibmcloud",
            "BUILD_ID": "1",
            "BASE_DOMAIN": settings["base_domain"],
            "RESOURCE_GROUP": settings["resource_group"],
            "CONTROL_PLANE_REPLICAS": str(settings["masters"]),
            "CONTROL_PLANE_INSTANCE_TYPE": settings["master_type"],
            "WORKERS": str(settings["workers"]),
            "COMPUTE_NODE_TYPE": settings["worker_type"],
            "SIZE_VARIANT": "compact" if settings["workers"] == 0 else "default",
            "ZONES_COUNT": str(len(settings["zones"])),
            "PUBLISH": "External",
            "RELEASE_IMAGE_LATEST": settings["release_image"],
            "OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE": settings["release_image"],
            "CUSTOM_OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE": settings["release_image"],
            "OPENSHIFT_INSTALL_PROMTAIL_ON_BOOTSTRAP": "false",
            "OPENSHIFT_INSTALL_GATHER_BOOTSTRAP": "false",
            "KUBECONFIG": "/dev/null",
        }
    )
    return values


def execute(step: str, root: Path, runtime: Path, values: dict) -> None:
    # Raw upstream output can contain credentials. It stays off Forge's public stdout.
    child_env = {
        **os.environ,
        **values,
        "HOME": str(runtime),
        "TMPDIR": str(runtime),
        "FORGE_RUNTIME_DIR": str(runtime),
    }
    log_path = root / "logs" / f"{runtime.name}-{step}.log"
    with log_path.open("w") as log:
        result = subprocess.run(
            ["bash", str(root / "release" / f"{step}-commands.sh")],
            cwd=runtime,
            env=child_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(f"Release step {step} failed; inspect the internal workspace logs")


if __name__ == "__main__":
    run.main()
