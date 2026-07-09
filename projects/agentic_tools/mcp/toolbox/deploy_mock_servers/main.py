"""
Deploy MCP-compatible mock servers on Kubernetes.

Supports both single-server (count=1) and multi-server (count>1) scenarios
using programmatic manifest generation. Each server gets a unique name
(e.g. mock-server-1 .. mock-server-N), its own Service, and configurable
tool count.

All resources are labeled for easy bulk cleanup between test levels.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger(__name__)

MOCK_MCP_LABEL = "forge.openshift.io/component=mock-mcp"


@entrypoint
def run(
    *,
    namespace: str,
    count: int,
    image: str,
    name_prefix: str = "mock-server",
    tools_per_server: int = 10,
    labels: dict[str, str] | None = None,
    node_selector: dict[str, str] | None = None,
    tolerations: list[dict[str, str]] | None = None,
    rollout_timeout: str = "120s",
) -> int:
    """Deploy mock servers and wait for readiness."""
    execute_tasks(locals())
    return 0


@task
def generate_and_apply_manifests(args, ctx):
    """Generate YAML manifests for all servers and apply them."""
    merged_labels = dict(args.labels) if args.labels else {}
    merged_labels["forge.openshift.io/component"] = "mock-mcp"

    ctx.names = [f"{args.name_prefix}-{i}" for i in range(1, args.count + 1)]

    all_manifests = []
    for name in ctx.names:
        manifest = _generate_server_manifest(
            name=name,
            namespace=args.namespace,
            image=args.image,
            tools_per_server=args.tools_per_server,
            labels=merged_labels,
            node_selector=args.node_selector,
            tolerations=args.tolerations,
        )
        all_manifests.append(manifest)

    combined_yaml = "---\n".join(all_manifests)

    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "mock-servers.yaml").write_text(combined_yaml, encoding="utf-8")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write(combined_yaml)
        tmp_path = tmp.name
    oc("apply", "-f", tmp_path)
    Path(tmp_path).unlink(missing_ok=True)

    return f"Applied {args.count} server manifest(s)"


@retry(attempts=24, delay=5, backoff=1.0)
@task
def wait_for_rollout(args, ctx):
    """Wait for all server deployments to become ready."""
    result = oc(
        "get",
        "deployment",
        "-n",
        args.namespace,
        "-l",
        MOCK_MCP_LABEL,
        "-o",
        "jsonpath={.items[?(@.status.readyReplicas==1)].metadata.name}",
        check=False,
    )
    ready_count = len(result.stdout.split()) if result.stdout.strip() else 0

    if ready_count >= args.count:
        ctx.server_names = ctx.names
        return f"All {args.count} server(s) ready"

    return (False, f"Servers ready: {ready_count}/{args.count}")


@always
@task
def capture_artifacts(args, ctx):
    """Capture deployment state for post-mortem diagnostics."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    _capture_to_file(
        artifacts_dir / "deployments.yaml",
        "get",
        "deployment",
        "-n",
        args.namespace,
        "-l",
        MOCK_MCP_LABEL,
        "-o",
        "yaml",
    )
    _capture_to_file(
        artifacts_dir / "pods.txt",
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        MOCK_MCP_LABEL,
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "events.txt",
        "get",
        "events",
        "-n",
        args.namespace,
        "--sort-by=.lastTimestamp",
    )

    return "Captured mock server state"


def cleanup_servers(*, namespace: str) -> None:
    """Delete all mock MCP server deployments and services by label."""
    logger.info("Cleaning up mock MCP servers (label=%s)...", MOCK_MCP_LABEL)
    oc(
        "delete",
        "deployment,service",
        "-n",
        namespace,
        "-l",
        MOCK_MCP_LABEL,
        "--wait=false",
        "--ignore-not-found=true",
        check=False,
    )
    _wait_for_deletion(namespace=namespace, resource="deployment", timeout_seconds=120)
    logger.info("Mock MCP server cleanup complete")


def restart_servers(
    *,
    namespace: str,
    count: int = 1,
    name_prefix: str = "mock-server",
    rollout_timeout: str = "120s",
) -> None:
    """Restart mock server deployment(s) for clean state between tests."""
    names = [f"{name_prefix}-{i}" for i in range(1, count + 1)]
    for name in names:
        logger.info("Restarting deployment/%s in %s", name, namespace)
        oc("rollout", "restart", f"deployment/{name}", "-n", namespace, check=False)

    for name in names:
        oc(
            "rollout",
            "status",
            f"deployment/{name}",
            "-n",
            namespace,
            f"--timeout={rollout_timeout}",
        )
    logger.info("All %d server(s) restarted and ready", len(names))


def _wait_for_deletion(*, namespace: str, resource: str, timeout_seconds: int = 120) -> None:
    """Wait until no resources with the mock-mcp label remain."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = oc(
            "get",
            resource,
            "-n",
            namespace,
            "-l",
            MOCK_MCP_LABEL,
            "--no-headers",
            check=False,
        )
        remaining = (
            len([line for line in result.stdout.strip().split("\n") if line.strip()])
            if result.stdout.strip()
            else 0
        )
        if remaining == 0:
            return
        time.sleep(3)
    logger.warning("Timed out waiting for %s deletion", resource)


def _generate_server_manifest(
    *,
    name: str,
    namespace: str,
    image: str,
    tools_per_server: int,
    labels: dict[str, str],
    node_selector: dict[str, str] | None = None,
    tolerations: list[dict[str, str]] | None = None,
) -> str:
    """Generate a YAML manifest for a single mock server Deployment + Service."""
    label_set = {"app": name}
    label_set.update(labels)

    pod_spec: dict[str, Any] = {
        "containers": [
            {
                "name": "server",
                "image": image,
                "imagePullPolicy": "Always",
                "args": ["--addr", ":8080"],
                "env": [
                    {"name": "GOGC", "value": "off"},
                    {"name": "NUM_TOOLS", "value": str(tools_per_server)},
                ],
                "ports": [{"containerPort": 8080, "name": "http"}],
                "readinessProbe": {
                    "tcpSocket": {"port": 8080},
                    "initialDelaySeconds": 2,
                    "periodSeconds": 5,
                    "timeoutSeconds": 2,
                },
            }
        ],
    }
    if node_selector:
        pod_spec["nodeSelector"] = node_selector
    if tolerations:
        pod_spec["tolerations"] = tolerations

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": label_set,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": label_set},
                "spec": pod_spec,
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": label_set,
        },
        "spec": {
            "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
            "selector": {"app": name},
            "type": "ClusterIP",
        },
    }

    return yaml.safe_dump_all([deployment, service], sort_keys=False)


def _capture_to_file(path: Path, *oc_args: str) -> None:
    """Best-effort capture of oc output to a file."""
    result = oc(*oc_args, check=False, log_stdout=False)
    if result.returncode == 0 and result.stdout:
        path.write_text(result.stdout, encoding="utf-8")
