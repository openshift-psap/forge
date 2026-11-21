"""
Capture Cluster State

Saves a snapshot of general cluster-level state for post-mortem debugging:
OCP version, nodes, machines, CSVs, and the Forge git revision.

Can be run standalone:
    ./bin/run_toolbox cluster capture_cluster_state
"""

from __future__ import annotations

import logging
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, shell, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger("DSL")


def _capture(description: str, path: Path, *command: str) -> str:
    try:
        result = oc(*command, check=False, log_stdout=False, log_stderr=False, stdout_dest=path)
    except Exception as exc:
        logger.warning("Failed to capture %s: %s", description, exc)
        return f"Failed to capture {description}"
    if result.returncode:
        logger.warning(
            "Failed to capture %s (oc exited with status %d)", description, result.returncode
        )
        return f"Failed to capture {description}"
    return f"Captured {description} to {path}"


def _capture_shell(description: str, path: Path, command: str) -> str:
    try:
        result = shell.run(command, check=False, log_stdout=False, stdout_dest=path)
    except Exception as exc:
        logger.warning("Failed to capture %s: %s", description, exc)
        return f"Failed to capture {description}"
    if result.returncode:
        logger.warning(
            "Failed to capture %s (exited with status %d)", description, result.returncode
        )
        return f"Failed to capture {description}"
    return f"Captured {description} to {path}"


@entrypoint
def run() -> None:
    """Capture general cluster-level state for debugging.

    Saves OCP version info, node and machine listings, ClusterServiceVersions,
    and the Forge git revision to the artifact directory.
    """
    return execute_tasks(locals())


@task
def capture_ocp_version(args, ctx):
    """Capture OpenShift version identifiers."""
    dest = Path(args.artifact_dir)
    dest.mkdir(parents=True, exist_ok=True)

    results = []
    results.append(
        _capture_shell(
            "OCP version string",
            dest / "ocp.version",
            "oc version -o json | jq --raw-output '.openshiftVersion'",
        )
    )
    results.append(_capture("OCP version YAML", dest / "ocp_version.yml", "version", "-oyaml"))
    results.append(
        _capture(
            "ClusterVersion",
            dest / "ocp_clusterversion.yml",
            "get",
            "clusterversion/version",
            "-oyaml",
        )
    )
    return "; ".join(results)


@task
def capture_nodes(args, ctx):
    """Capture cluster node state."""
    dest = Path(args.artifact_dir)

    results = []
    results.append(_capture("nodes status", dest / "nodes.status", "get", "nodes", "-owide"))
    results.append(_capture("nodes YAML", dest / "nodes.yaml", "get", "nodes", "-oyaml"))
    results.append(_capture("nodes JSON", dest / "nodes.json", "get", "nodes", "-ojson"))
    results.append(_capture_shell("nodes description", dest / "nodes.descr", "oc describe nodes"))
    return "; ".join(results)


@task
def capture_machines(args, ctx):
    """Capture cluster machine state."""
    dest = Path(args.artifact_dir)

    results = []
    results.append(
        _capture(
            "machines status",
            dest / "machines.status",
            "get",
            "machines",
            "-n",
            "openshift-machine-api",
            "-owide",
        )
    )
    results.append(
        _capture(
            "machines YAML",
            dest / "machines.yaml",
            "get",
            "machines",
            "-n",
            "openshift-machine-api",
            "-oyaml",
        )
    )
    results.append(
        _capture_shell(
            "machines description",
            dest / "machines.descr",
            "oc describe machines -n openshift-machine-api",
        )
    )
    return "; ".join(results)


@task
def capture_csvs(args, ctx):
    """Capture ClusterServiceVersions (excluding OLM copies)."""
    dest = Path(args.artifact_dir)

    return _capture_shell(
        "CSVs",
        dest / "csv.status",
        "oc get csv -A --show-labels | grep -v olm.copiedFrom",
    )


@task
def capture_forge_version(args, ctx):
    """Capture the Forge git revision and last commit."""
    dest = Path(args.artifact_dir)

    results = []
    results.append(
        _capture_shell(
            "Forge git version",
            dest / "forge.git_version",
            "git describe HEAD --long --always",
        )
    )
    results.append(
        _capture_shell(
            "Forge last git commit",
            dest / "forge.git_commit",
            "git show --no-patch",
        )
    )
    return "; ".join(results)


if __name__ == "__main__":
    run.main()
