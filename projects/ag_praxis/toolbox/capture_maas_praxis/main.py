from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger("DSL")

DEPLOYMENTS_TO_CAPTURE = ["payload-processing", "payload-pre-processing"]


def _capture(description: str, path, *command: str) -> str:
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


@entrypoint
def run(
    *,
    ingress_namespace: str = "openshift-ingress",
    tenant_namespace: str = "models-as-a-service",
    tenant_name: str = "default-tenant",
):
    """Capture MaaS praxis payload-processing state for debugging.

    Args:
        ingress_namespace: Namespace where payload-processing deployments live.
        tenant_namespace: Namespace of the MaasTenantConfig resource.
        tenant_name: Name of the MaasTenantConfig resource.
    """
    return execute_tasks(locals())


@task
def capture_overview(args, ctx):
    """Capture wide listing of pods and deployments in the ingress namespace."""
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    results = []
    results.append(
        _capture(
            "deployments wide listing",
            artifacts / "deployments_wide.txt",
            "get",
            "deployments",
            "-n",
            args.ingress_namespace,
            "-owide",
        )
    )
    results.append(
        _capture(
            "pods wide listing",
            artifacts / "pods_wide.txt",
            "get",
            "pods",
            "-n",
            args.ingress_namespace,
            "-owide",
        )
    )
    return "; ".join(results)


@task
def capture_deployments(args, ctx):
    """Capture YAML and describe output for each payload-processing deployment."""
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    results = []
    for deploy_name in DEPLOYMENTS_TO_CAPTURE:
        results.append(
            _capture(
                f"{deploy_name} yaml",
                artifacts / f"{deploy_name}.yaml",
                "get",
                "deployment",
                deploy_name,
                "-n",
                args.ingress_namespace,
                "-o",
                "yaml",
            )
        )
        results.append(
            _capture(
                f"{deploy_name} describe",
                artifacts / f"{deploy_name}_describe.txt",
                "describe",
                "deployment",
                deploy_name,
                "-n",
                args.ingress_namespace,
            )
        )
    return "; ".join(results)


@task
def capture_pods(args, ctx):
    """Capture status and describe output for payload-processing pods."""
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    results = []
    for deploy_name in DEPLOYMENTS_TO_CAPTURE:
        selector = f"app={deploy_name}"
        results.append(
            _capture(
                f"{deploy_name} pods status",
                artifacts / f"{deploy_name}_pods.txt",
                "get",
                "pods",
                "-l",
                selector,
                "-n",
                args.ingress_namespace,
                "-owide",
            )
        )
        results.append(
            _capture(
                f"{deploy_name} pods describe",
                artifacts / f"{deploy_name}_pods_describe.txt",
                "describe",
                "pods",
                "-l",
                selector,
                "-n",
                args.ingress_namespace,
            )
        )
    return "; ".join(results)


@task
def capture_maas_tenant_config(args, ctx):
    """Capture the MaasTenantConfig resource."""
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    results = []
    results.append(
        _capture(
            "MaasTenantConfig yaml",
            artifacts / "maastenantconfig.yaml",
            "get",
            "maastenantconfig",
            args.tenant_name,
            "-n",
            args.tenant_namespace,
            "-o",
            "yaml",
        )
    )
    results.append(
        _capture(
            "MaasTenantConfig describe",
            artifacts / "maastenantconfig_describe.txt",
            "describe",
            "maastenantconfig",
            args.tenant_name,
            "-n",
            args.tenant_namespace,
        )
    )
    return "; ".join(results)


if __name__ == "__main__":
    run.main()
