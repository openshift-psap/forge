#!/usr/bin/env python3

from __future__ import annotations

from projects.core.dsl import always, entrypoint, execute_tasks, task
from projects.core.dsl.utils import write_text
from projects.core.dsl.utils.k8s import (
    oc,
    oc_apply,
)
from projects.rhoai.toolbox.apply_datasciencecluster.utils import render_datasciencecluster


@entrypoint
def run(
    *,
    datasciencecluster_name: str,
    namespace: str,
    components: list[str],
) -> int:
    """
    Apply the llm_d DataScienceCluster manifest.

    Args:
        datasciencecluster_name: Name of the DataScienceCluster
        namespace: Namespace for the DataScienceCluster
        components: List of components to enable (e.g., ["kserve", "codeflare"])
    """

    execute_tasks(locals())
    return 0


@task
def capture_initial_dsc(args, ctx):
    """Capture the DataScienceCluster object before any modifications"""

    # Ensure artifacts directory exists
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    result = oc(
        "get",
        "datasciencecluster",
        args.datasciencecluster_name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
    )

    if result.returncode == 0:
        write_text(artifacts_dir / "datasciencecluster-initial.yaml", result.stdout)
        return f"Captured initial DataScienceCluster {args.datasciencecluster_name}"
    else:
        write_text(
            artifacts_dir / "datasciencecluster-initial.yaml",
            "# DataScienceCluster did not exist initially\n",
        )
        return f"DataScienceCluster {args.datasciencecluster_name} did not exist initially"


@task
def wait_for_operator_deployments(args, ctx):
    """Wait for all deployments in redhat-ods-operator namespace to be ready"""

    # Wait for all deployments in redhat-ods-operator to be ready (5 minute timeout)
    result = oc(
        "wait",
        "--for=condition=Available",
        "--timeout=300s",
        "deployment",
        "--all",
        "-n",
        "redhat-ods-operator",
        check=False,
    )

    # Guard pattern: handle success case first and return early
    if result.returncode == 0:
        return "All deployments in redhat-ods-operator namespace are ready"

    # Handle failure case
    status_result = oc(
        "get",
        "deployments",
        "-n",
        "redhat-ods-operator",
        "-o",
        "wide",
        check=False,
    )

    error_msg = "Timeout waiting for deployments in redhat-ods-operator namespace to be ready"
    if status_result.returncode == 0:
        error_msg += f"\n\nCurrent deployment status:\n{status_result.stdout}"

    raise RuntimeError(error_msg)


@task
def apply_datasciencecluster(args, ctx):
    """Render and apply the DataScienceCluster manifest"""

    manifest = render_datasciencecluster(
        datasciencecluster_name=args.datasciencecluster_name,
        namespace=args.namespace,
        components=args.components,
    )

    # Ensure the src directory exists
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    oc_apply(src_dir / "datasciencecluster.yaml", manifest)
    return "DataScienceCluster applied"


@always
@task
def capture_final_dsc(args, ctx):
    """Capture the DataScienceCluster object after all operations (always runs)"""

    # Ensure artifacts directory exists
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    result = oc(
        "get",
        "datasciencecluster",
        args.datasciencecluster_name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
    )

    if result.returncode == 0:
        write_text(artifacts_dir / "datasciencecluster-final.yaml", result.stdout)
        return f"Captured final DataScienceCluster {args.datasciencecluster_name}"
    else:
        write_text(
            artifacts_dir / "datasciencecluster-final.yaml", "# DataScienceCluster not found\n"
        )
        return f"DataScienceCluster {args.datasciencecluster_name} not found"


if __name__ == "__main__":
    run.main()
