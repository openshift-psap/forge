from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc, oc_get_json

logger = logging.getLogger("DSL")

GATEWAY_NAME_LABEL = "gateway.networking.k8s.io/gateway-name"


@dataclass(frozen=True)
class DeploymentTarget:
    """A deployment to capture: name, namespace, pod selector and dedicated artifact directory."""

    name: str
    namespace: str
    selector: str
    dirname: str

    def artifact_dir(self, artifacts_root: Path) -> Path:
        """Return the dedicated artifact directory of this component."""
        return artifacts_root / self.dirname


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


def _resolve_gateway_deployment_name(gateway_name: str, namespace: str) -> str:
    """Resolve the deployment backing a Gateway (name is '{gateway}-{gatewayclass}')."""
    deployments = oc_get_json(
        "deployment",
        selector=f"{GATEWAY_NAME_LABEL}={gateway_name}",
        namespace=namespace,
        ignore_not_found=True,
    )
    items = (deployments or {}).get("items", [])
    if not items:
        logger.warning(
            "No deployment found for gateway %s in namespace %s, falling back to its name",
            gateway_name,
            namespace,
        )
        return gateway_name
    return items[0]["metadata"]["name"]


@entrypoint
def run(
    *,
    ingress_namespace: str = "openshift-ingress",
    tenant_namespace: str = "models-as-a-service",
    tenant_name: str = "default-tenant",
    kuadrant_namespace: str = "kuadrant-system",
    gateway_name: str = "maas-default-gateway",
):
    """Capture the state of the inference hot-path deployments for debugging.

    Covers the payload-processing deployments, the MaaS gateway, and the kuadrant
    auth/ratelimiting backends (authorino, limitador), plus the MaasTenantConfig.

    Each component's files are saved in a dedicated directory under the artifacts
    directory (e.g. artifacts/authorino/deployment.yaml), with its pod logs under
    a logs/ subdirectory.

    Args:
        ingress_namespace: Namespace where the gateway and payload-processing deployments live.
        tenant_namespace: Namespace of the MaasTenantConfig resource.
        tenant_name: Name of the MaasTenantConfig resource.
        kuadrant_namespace: Namespace where the authorino and limitador deployments live.
        gateway_name: Name of the MaaS Gateway resource.
    """
    return execute_tasks(locals())


def build_hot_path_targets(
    ingress_namespace: str, kuadrant_namespace: str, gateway_name: str
) -> list[DeploymentTarget]:
    """Build the list of inference hot-path deployments (payload extprocs, gateway, kuadrant backends)."""
    return [
        DeploymentTarget(
            name="payload-processing",
            namespace=ingress_namespace,
            selector="app=payload-processing",
            dirname="payload-processing",
        ),
        DeploymentTarget(
            name="payload-pre-processing",
            namespace=ingress_namespace,
            selector="app=payload-pre-processing",
            dirname="payload-pre-processing",
        ),
        DeploymentTarget(
            name=_resolve_gateway_deployment_name(gateway_name, ingress_namespace),
            namespace=ingress_namespace,
            selector=f"{GATEWAY_NAME_LABEL}={gateway_name}",
            dirname="gateway",
        ),
        DeploymentTarget(
            name="authorino",
            namespace=kuadrant_namespace,
            selector="authorino-resource=authorino",
            dirname="authorino",
        ),
        DeploymentTarget(
            name="limitador-limitador",
            namespace=kuadrant_namespace,
            selector="app=limitador",
            dirname="limitador",
        ),
    ]


@task
def resolve_hot_path_deployments(args, ctx):
    """Resolve the hot-path deployments to capture (gateway deployment name is discovered by label)."""
    ctx.hot_path_deployments = build_hot_path_targets(
        args.ingress_namespace, args.kuadrant_namespace, args.gateway_name
    )
    names = ", ".join(f"{t.name} ({t.namespace})" for t in ctx.hot_path_deployments)
    return f"Hot-path deployments to capture: {names}"


@task
def capture_overview(args, ctx):
    """Capture wide listing of pods and deployments in the ingress and kuadrant namespaces."""
    artifacts = args.artifact_dir / "artifacts"

    namespaces = {
        "ingress": args.ingress_namespace,
        "kuadrant": args.kuadrant_namespace,
    }
    results = []
    for dirname, namespace in namespaces.items():
        namespace_dir = artifacts / dirname
        namespace_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"deployments wide listing ({namespace})",
                namespace_dir / "deployments_wide.txt",
                "get",
                "deployments",
                "-n",
                namespace,
                "-owide",
            )
        )
        results.append(
            _capture(
                f"pods wide listing ({namespace})",
                namespace_dir / "pods_wide.txt",
                "get",
                "pods",
                "-n",
                namespace,
                "-owide",
            )
        )
    return "; ".join(results)


@task
def capture_deployments(args, ctx):
    """Capture YAML and describe output for each hot-path deployment."""
    artifacts = args.artifact_dir / "artifacts"

    results = []
    for target in ctx.hot_path_deployments:
        target_dir = target.artifact_dir(artifacts)
        target_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"{target.name} yaml",
                target_dir / "deployment.yaml",
                "get",
                "deployment",
                target.name,
                "-n",
                target.namespace,
                "-o",
                "yaml",
            )
        )
        results.append(
            _capture(
                f"{target.name} describe",
                target_dir / "deployment_describe.txt",
                "describe",
                "deployment",
                target.name,
                "-n",
                target.namespace,
            )
        )
    return "; ".join(results)


@task
def capture_pods(args, ctx):
    """Capture status and describe output for the hot-path deployment pods."""
    artifacts = args.artifact_dir / "artifacts"

    results = []
    for target in ctx.hot_path_deployments:
        target_dir = target.artifact_dir(artifacts)
        target_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"{target.name} pods status",
                target_dir / "pods.txt",
                "get",
                "pods",
                "-l",
                target.selector,
                "-n",
                target.namespace,
                "-owide",
            )
        )
        results.append(
            _capture(
                f"{target.name} pods describe",
                target_dir / "pods_describe.txt",
                "describe",
                "pods",
                "-l",
                target.selector,
                "-n",
                target.namespace,
            )
        )
    return "; ".join(results)


@task
def capture_pod_logs(args, ctx):
    """Capture logs for the hot-path deployment pods.

    `--tail=-1` is mandatory: `oc logs` defaults to only the last 10 lines when a
    selector is given, which would truncate the gateway's Envoy access log — the
    record of every request the gateway rejected, and the only place where the
    failing filter is named (`%RESPONSE_CODE_DETAILS%`).

    No tail cap is needed otherwise: the pods are bumped fresh before each benchmark
    endpoint, so the logs are bounded by one benchmark window.
    """
    artifacts = args.artifact_dir / "artifacts"

    results = []
    for target in ctx.hot_path_deployments:
        logs_dir = target.artifact_dir(artifacts) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"{target.name} logs",
                logs_dir / "pods.log",
                "logs",
                "-l",
                target.selector,
                "-n",
                target.namespace,
                "--all-containers",
                "--prefix",
                "--tail=-1",
            )
        )
        results.append(
            _capture(
                f"{target.name} previous logs",
                logs_dir / "pods_previous.log",
                "logs",
                "-l",
                target.selector,
                "-n",
                target.namespace,
                "--all-containers",
                "--prefix",
                "--previous",
                "--tail=-1",
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
