#!/usr/bin/env python3
"""
Clean up MLflow per-test resources from the cluster.

Removes everything deployed by apply_infrastructure in reverse order:
1. MLflow CR (stops the MLflow server)
2. Per-project MLflowConfig and RBAC
3. Leftover k6 resources and the MinIO bucket creation Job
4. PostgreSQL and MinIO Deployments, Services, and PVCs
5. Per-test credentials after the workloads and PVCs are gone

Does NOT disable the MLflow operator or delete the namespace —
those are handled by cleanup_platform.
"""

from __future__ import annotations

import logging

from projects.core.dsl import always, entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc, oc
from projects.mlflow.library.credentials import (
    ARTIFACT_CONNECTION_SECRET,
    EXPORTER_SECRET,
    MINIO_SECRET,
    MLFLOW_DB_SECRET,
    MLFLOW_S3_SECRET,
    POSTGRES_SECRET,
)

logger = logging.getLogger(__name__)


K6_LABEL_SELECTOR = "forge.openshift.io/project=mlflow"
STORAGE_RESOURCES = (
    "pvc/postgresql-pvc",
    "pvc/minio-pvc",
    "deployment/postgresql",
    "deployment/minio",
)


@entrypoint
def run(
    *,
    namespace: str = "mlflow-perf-testing",
    operator_namespace: str = "redhat-ods-applications",
) -> int:
    """Remove all per-test MLflow resources from the cluster."""
    execute_tasks(locals())
    return 0


@task
def delete_mlflow_cr(args, ctx):
    """Delete the MLflow custom resource (stops the server pods)"""
    best_effort_oc(
        "delete",
        "mlflow",
        "mlflow",
        "-n",
        args.operator_namespace,
        "--ignore-not-found=true",
        "--timeout=120s",
        description="MLflow CR",
    )

    result = oc(
        "get",
        "pods",
        "-n",
        args.operator_namespace,
        "-l",
        "app=mlflow",
        "-o",
        "name",
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        best_effort_oc(
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app=mlflow",
            "-n",
            args.operator_namespace,
            "--timeout=60s",
            description="MLflow pod termination",
        )

    return "MLflow CR deleted"


@task
def delete_mlflow_podmonitor(args, ctx):
    """Delete MLflow PodMonitor from the operator namespace"""
    best_effort_oc(
        "delete",
        "podmonitor",
        "mlflow-metrics",
        "-n",
        args.operator_namespace,
        "--ignore-not-found=true",
        description="MLflow PodMonitor",
    )
    return "MLflow PodMonitor deleted"


@task
def delete_per_project_config(args, ctx):
    """Delete the per-project MLflowConfig."""
    best_effort_oc(
        "delete",
        "mlflowconfig",
        "mlflow",
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        description="MLflowConfig",
    )
    return "Per-project config deleted"


@task
def delete_rbac(args, ctx):
    """Delete the RBAC RoleBinding for service account access"""
    best_effort_oc(
        "delete",
        "rolebinding",
        "mlflow-perf-testing-access",
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        description="MLflow RBAC RoleBinding",
    )
    return "RBAC RoleBinding deleted"


@task
def delete_k6_resources(args, ctx):
    """Delete any leftover k6 Jobs, ConfigMaps, and read-data PVCs.

    Safety net for runs interrupted mid-matrix.
    """
    for kind in ("job", "configmap", "pod"):
        result = oc(
            "get",
            kind,
            "-n",
            args.namespace,
            "-l",
            K6_LABEL_SELECTOR,
            "-o",
            "name",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue
        best_effort_oc(
            "delete",
            kind,
            "-n",
            args.namespace,
            "-l",
            K6_LABEL_SELECTOR,
            "--ignore-not-found=true",
            description=f"leftover k6 {kind}s",
        )
    for selector, description in (("forge.openshift.io/read-data=k6", "leftover k6 read PVCs"),):
        best_effort_oc(
            "delete",
            "pvc",
            "-n",
            args.namespace,
            "-l",
            selector,
            "--ignore-not-found=true",
            description=description,
        )
    return "Leftover k6 resources deleted"


@task
def delete_bucket_job(args, ctx):
    """Delete the MinIO bucket creation job"""
    best_effort_oc(
        "delete",
        "job",
        "minio-create-bucket",
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        "--cascade=foreground",
        description="MinIO bucket creation job",
    )
    return "Bucket creation job deleted"


@task
def delete_postgresql(args, ctx):
    """Delete the PostgreSQL workload and persistent storage."""
    for kind, name in [
        ("deployment", "postgresql"),
        ("service", "postgresql"),
        ("pvc", "postgresql-pvc"),
        ("servicemonitor", "postgresql-metrics"),
    ]:
        best_effort_oc(
            "delete",
            kind,
            name,
            "-n",
            args.namespace,
            "--ignore-not-found=true",
            description=f"PostgreSQL {kind}",
        )
    return "PostgreSQL resources deleted"


@task
def delete_minio(args, ctx):
    """Delete MinIO resources (Deployment, Service, PVC)"""
    for kind, name in [
        ("deployment", "minio"),
        ("service", "minio"),
        ("pvc", "minio-pvc"),
    ]:
        best_effort_oc(
            "delete",
            kind,
            name,
            "-n",
            args.namespace,
            "--ignore-not-found=true",
            description=f"MinIO {kind}",
        )
    return "MinIO resources deleted"


@task
def wait_for_storage_removal(args, ctx):
    """Wait for data PVCs and their deployments to be removed."""
    for resource in STORAGE_RESOURCES:
        oc(
            "wait",
            "--for=delete",
            resource,
            "-n",
            args.namespace,
            "--timeout=180s",
            check=False,
        )


@task
def delete_test_credentials(args, ctx):
    """Remove credentials after workloads can no longer use them."""
    remaining = [
        oc(
            "get",
            resource,
            "-n",
            args.namespace,
            "--ignore-not-found=true",
            "-o",
            "name",
        ).stdout.strip()
        for resource in (*STORAGE_RESOURCES, "job/minio-create-bucket")
    ]
    remaining.extend(
        oc("get", "pods", "-n", namespace, "-l", selector, "-o", "name").stdout.strip()
        for namespace, selector in (
            (args.namespace, "app in (postgresql,minio)"),
            (args.namespace, "job-name=minio-create-bucket"),
            (args.operator_namespace, "app=mlflow"),
        )
    )
    if resources := " ".join(filter(None, remaining)):
        raise RuntimeError(
            f"Cannot remove MLflow test credentials while resources remain: {resources}"
        )

    oc(
        "delete",
        "secret",
        MLFLOW_DB_SECRET,
        MLFLOW_S3_SECRET,
        "-n",
        args.operator_namespace,
        "--ignore-not-found=true",
    )
    oc(
        "delete",
        "secret",
        ARTIFACT_CONNECTION_SECRET,
        POSTGRES_SECRET,
        EXPORTER_SECRET,
        MINIO_SECRET,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
    )
    return "Removed MLflow test credentials"


@always
@task
def capture_cleanup_state(args, ctx):
    """Verify cleanup by capturing remaining resource state"""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    _capture_to_file(
        artifacts_dir / "remaining-pods-perf-testing.txt",
        "get",
        "pods",
        "-n",
        args.namespace,
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "remaining-pvcs.txt",
        "get",
        "pvc",
        "-n",
        args.namespace,
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "remaining-secrets-operator-ns.txt",
        "get",
        "secret",
        "-n",
        args.operator_namespace,
        "-l",
        "app in (mlflow)",
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "remaining-mlflow-pods.txt",
        "get",
        "pods",
        "-n",
        args.operator_namespace,
        "-l",
        "app=mlflow",
        "-o",
        "wide",
    )

    return "Captured post-cleanup state"


def _capture_to_file(path, *oc_args: str) -> None:
    oc(*oc_args, check=False, log_stdout=False, stdout_dest=path)


if __name__ == "__main__":
    run.main()
