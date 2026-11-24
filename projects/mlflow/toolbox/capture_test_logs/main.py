"""Capture logs used to diagnose one MLflow load-test iteration."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from projects.core.dsl import EarlyReturn, entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import capture_pod_logs, oc

logger = logging.getLogger(__name__)

_OPERATOR_NAMESPACE = "redhat-ods-applications"
_GATEWAY_NAMESPACE = "openshift-ingress"
_GATEWAY_DEPLOYMENT = "data-science-gateway-data-science-gateway-class"


@entrypoint
def run(
    *,
    namespace: str,
    since_time: str | datetime | None = None,
    tenant_namespaces: str | list[str] | None = None,
    operator_namespace: str = _OPERATOR_NAMESPACE,
    failure_type: str | None = None,
) -> Path:
    """Capture logs and Kubernetes state before test resources are removed.

    Args:
        namespace: Namespace hosting the test workloads
        since_time: Only capture gateway logs after this ISO 8601 timestamp
        tenant_namespaces: JSON array of tenant namespaces to inspect
        operator_namespace: Namespace hosting MLflow server pods
        failure_type: Capture pre-cleanup failure diagnostics for this exception type
    """
    if isinstance(since_time, datetime):
        since_time = since_time.isoformat()
    if isinstance(tenant_namespaces, list):
        tenant_namespaces = json.dumps(tenant_namespaces)
    return execute_tasks(locals()).artifact_dir


@task
def prepare_capture(args, ctx):
    """Resolve capture inputs and handle the pre-cleanup diagnostics mode."""
    ctx.tenant_namespaces = json.loads(args.tenant_namespaces) if args.tenant_namespaces else []
    if args.failure_type:
        _capture_failure_diagnostics(
            output_dir=args.artifact_dir,
            namespace=args.namespace,
            tenant_namespaces=ctx.tenant_namespaces,
            failure_type=args.failure_type,
            operator_namespace=args.operator_namespace,
        )
        return EarlyReturn("Captured pre-cleanup failure diagnostics")
    ctx.since_time = datetime.fromisoformat(args.since_time) if args.since_time else None


def _capture_failure_diagnostics(
    *,
    output_dir: Path,
    namespace: str,
    tenant_namespaces: list[str] | None = None,
    failure_type: str | None = None,
    operator_namespace: str = _OPERATOR_NAMESPACE,
) -> None:
    """Capture Kubernetes state directly to a caller-selected directory.

    This can be called by orchestration exception handlers before cleanup,
    including when failure occurs before the normal test artifact directory
    has been entered.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    if failure_type:
        (output_dir / "failure-context.txt").write_text(
            f"Failure type: {failure_type}\n"
            "Diagnostics captured before matrix-cell infrastructure cleanup.\n",
            encoding="utf-8",
        )

    diagnostics_dir = output_dir / "kubernetes_diagnostics"
    _capture_namespace_diagnostics(
        namespace=namespace,
        output_dir=diagnostics_dir,
        include_pods=True,
    )
    _capture_namespace_diagnostics(
        namespace=operator_namespace,
        output_dir=diagnostics_dir,
        include_pods=True,
        pod_selector="app=mlflow",
    )
    _capture_to_file(
        diagnostics_dir / "mlflow-deployment-describe.txt",
        "describe",
        "deployment",
        "mlflow",
        "-n",
        operator_namespace,
    )

    namespace_dir = diagnostics_dir / namespace
    for resource, name, filename in (
        ("deployment", "postgresql", "postgresql-deployment-describe.txt"),
        ("pvc", "postgresql-pvc", "postgresql-pvc-describe.txt"),
        ("deployment", "minio", "minio-deployment-describe.txt"),
        ("pvc", "minio-pvc", "minio-pvc-describe.txt"),
    ):
        _capture_to_file(
            namespace_dir / filename,
            "describe",
            resource,
            name,
            "-n",
            namespace,
        )

    _capture_to_file(
        namespace_dir / "pvcs-wide.txt",
        "get",
        "pvc",
        "-n",
        namespace,
        "-o",
        "wide",
    )
    _capture_to_file(
        diagnostics_dir / "cluster-nodes-wide.txt",
        "get",
        "nodes",
        "-o",
        "wide",
    )

    for tenant_namespace in tenant_namespaces or []:
        _capture_namespace_diagnostics(
            namespace=tenant_namespace,
            output_dir=diagnostics_dir,
            include_pods=False,
        )


@task
def capture_test_namespace_logs(args, context):
    """Capture logs from all pods used by this test iteration."""
    output_dir = args.artifact_dir / "pod_logs"
    capture_pod_logs(namespace=args.namespace, output_dir=output_dir)
    return f"Captured test namespace pod logs in {output_dir}"


@task
def capture_mlflow_server_logs(args, context):
    """Capture current MLflow and OAuth proxy logs, plus previous MLflow logs."""
    output_dir = args.artifact_dir / "pod_logs"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = oc(
        "get",
        "pods",
        "-n",
        args.operator_namespace,
        "-l",
        "app=mlflow",
        "-o",
        "jsonpath={.items[*].metadata.name}",
        check=False,
        log_stdout=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        logger.warning("No MLflow server pods found in %s", args.operator_namespace)
        return "No MLflow server pods found"

    for pod_name in result.stdout.strip().split():
        for container in ("mlflow", "oauth-proxy"):
            destination = output_dir / f"{pod_name}_{container}.log"
            log_result = oc(
                "logs",
                "-n",
                args.operator_namespace,
                pod_name,
                "-c",
                container,
                "--previous=false",
                check=False,
                log_stdout=False,
                stdout_dest=destination,
            )
            if log_result.returncode != 0:
                logger.debug(
                    "Could not capture %s/%s logs: %s",
                    pod_name,
                    container,
                    log_result.stderr,
                )

        oc(
            "logs",
            "-n",
            args.operator_namespace,
            pod_name,
            "-c",
            "mlflow",
            "--previous=true",
            check=False,
            log_stdout=False,
            stdout_dest=output_dir / f"{pod_name}_mlflow_previous.log",
        )

    return "Captured MLflow server logs"


@task
def capture_gateway_logs(args, context):
    """Capture gateway and auth proxy logs, filtering gateway lines for MLflow."""
    output_dir = args.artifact_dir / "gateway_logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    _capture_gateway_access_logs(output_dir=output_dir, since_time=context.since_time)
    _capture_auth_proxy_logs(output_dir=output_dir, since_time=context.since_time)
    return f"Captured gateway logs in {output_dir}"


@task
def capture_kubernetes_diagnostics(args, context):
    """Capture pod state, probe details, and recent namespace events."""
    output_dir = args.artifact_dir / "kubernetes_diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True)

    _capture_namespace_diagnostics(
        namespace=args.namespace,
        output_dir=output_dir,
        include_pods=True,
    )
    _capture_namespace_diagnostics(
        namespace=args.operator_namespace,
        output_dir=output_dir,
        include_pods=True,
        pod_selector="app=mlflow",
    )
    _capture_to_file(
        output_dir / "mlflow-deployment-describe.txt",
        "describe",
        "deployment",
        "mlflow",
        "-n",
        args.operator_namespace,
    )

    for namespace in context.tenant_namespaces:
        _capture_namespace_diagnostics(
            namespace=namespace,
            output_dir=output_dir,
            include_pods=False,
        )

    return f"Captured Kubernetes diagnostics in {output_dir}"


def _capture_namespace_diagnostics(
    *,
    namespace: str,
    output_dir: Path,
    include_pods: bool,
    pod_selector: str | None = None,
) -> None:
    """Save pod descriptions and events while resources still exist."""
    namespace_dir = output_dir / namespace
    namespace_dir.mkdir(parents=True, exist_ok=True)

    if include_pods:
        pod_args = ["-n", namespace]
        if pod_selector:
            pod_args.extend(["-l", pod_selector])
        _capture_to_file(
            namespace_dir / "pods-wide.txt",
            "get",
            "pods",
            *pod_args,
            "-o",
            "wide",
        )
        _capture_to_file(
            namespace_dir / "pods-describe.txt",
            "describe",
            "pods",
            *pod_args,
        )

    _capture_to_file(
        namespace_dir / "events.txt",
        "get",
        "events",
        "-n",
        namespace,
        "--sort-by=.lastTimestamp",
        "-o",
        "wide",
    )


def _capture_to_file(path: Path, *oc_args: str) -> None:
    result = oc(
        *oc_args,
        check=False,
        log_stdout=False,
        log_stderr=False,
        stdout_dest=path,
    )
    if result.returncode != 0 and result.stderr:
        with path.open("a", encoding="utf-8") as output:
            output.write(f"\nCommand failed (exit {result.returncode}): oc {' '.join(oc_args)}\n")
            output.write(result.stderr)


def _capture_gateway_access_logs(*, output_dir: Path, since_time: datetime | None) -> None:
    all_logs = output_dir / "gateway_access_logs_all.txt"
    mlflow_logs = output_dir / "gateway_access_logs_mlflow.txt"

    logger.info("Capturing Data Science Gateway access logs")
    cmd = [
        "logs",
        "-n",
        _GATEWAY_NAMESPACE,
        f"deployment/{_GATEWAY_DEPLOYMENT}",
    ]
    if since_time is not None:
        cmd.append(f"--since-time={since_time.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    else:
        cmd.append("--tail=50000")

    result = oc(*cmd, check=False, log_stdout=False, stdout_dest=all_logs)
    if result.returncode != 0:
        logger.warning("Failed to capture gateway logs: %s", result.stderr)
        return

    try:
        with all_logs.open() as source, mlflow_logs.open("w") as destination:
            count = 0
            for line in source:
                if "mlflow" in line.lower():
                    destination.write(line)
                    count += 1
        logger.info("Filtered %d MLflow lines from gateway access logs", count)
    except OSError as error:
        logger.warning("Failed to filter gateway logs: %s", error)


def _capture_auth_proxy_logs(*, output_dir: Path, since_time: datetime | None) -> None:
    logger.info("Capturing kube-auth-proxy logs")
    cmd = ["logs", "-n", _GATEWAY_NAMESPACE, "deployment/kube-auth-proxy"]
    if since_time is not None:
        cmd.append(f"--since-time={since_time.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    else:
        cmd.append("--tail=50000")

    result = oc(
        *cmd,
        check=False,
        log_stdout=False,
        stdout_dest=output_dir / "kube_auth_proxy_logs.txt",
    )
    if result.returncode != 0:
        logger.warning("Failed to capture kube-auth-proxy logs: %s", result.stderr)


if __name__ == "__main__":
    run.main()
