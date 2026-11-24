"""Destroy the saved IBM Cloud installation using Forge DSL tasks."""

import json
import shutil
import tempfile
from pathlib import Path

from projects.core.dsl import EarlyReturn, entrypoint, execute_tasks, task

from .main import execute, profile_directory, state_directory


@entrypoint
def run(*, state_dir: str, profile_dir: str = "") -> dict:
    """Destroy a cluster using its saved release scripts and installer assets.

    Args:
        state_dir: The private persistent directory used by create.
        profile_dir: Read-only ibmcloud-ipi vault directory, resolved by Forge.
    """
    return execute_tasks(locals()).result


@task
def check_installation(args, ctx):
    """Check whether cloud teardown or only remaining local cleanup is needed."""
    ctx.root = state_directory(args.state_dir)
    ctx.cloud_destroyed = (ctx.root / "destroy.succeeded").exists()
    if ctx.cloud_destroyed:
        return "Cloud resources already destroyed; resume private output cleanup"
    if not (ctx.root / "install.started").exists():
        ctx.result = {"status": "not_started"}
        return EarlyReturn("Installation never started; no cloud teardown needed")
    ctx.profile = profile_directory(args.profile_dir)
    return "Installation requires teardown"


@task
def load_installer_state(args, ctx):
    """Load original inputs and verify the installer assets belong to this run."""
    if ctx.cloud_destroyed:
        return "Cloud teardown already completed; installer assets are no longer needed"
    ctx.values = json.loads((ctx.root / "inputs.json").read_text())
    assets = ctx.root / "install" / "installer"
    if not (assets / "metadata.json").is_file():
        raise RuntimeError(
            "Install was attempted but metadata is missing; inspect internal state and IBM Cloud"
        )
    metadata = json.loads((assets / "metadata.json").read_text())
    if metadata.get("clusterName") != f"forge-{ctx.values['UNIQUE_HASH']}":
        raise ValueError("Installer metadata does not match this job")
    ctx.values.update(SHARED_DIR=str(assets), CLUSTER_PROFILE_DIR=str(ctx.profile))
    ctx.runtime = Path(tempfile.mkdtemp(prefix="destroy-", dir=ctx.root))
    return "Original release inputs and installer assets loaded"


@task
def destroy_cluster(args, ctx):
    """Run the saved upstream teardown script, preserving files on failure."""
    if ctx.cloud_destroyed:
        return "Cloud teardown already completed; no cloud operations needed"
    execute("destroy", ctx.root, ctx.runtime, ctx.values)
    return "Cloud teardown completed"


@task
def remove_private_outputs(args, ctx):
    """Remove generated credentials and runtime files after successful teardown."""
    (ctx.root / "destroy.succeeded").touch()
    for path in list(ctx.root.iterdir()):
        if path.is_dir() and path.name != "release":
            shutil.rmtree(path)
    ctx.result = {"status": "destroyed"}
    return "Private runtime outputs removed; release scripts and inputs retained"


if __name__ == "__main__":
    run.main()
