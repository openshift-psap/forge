#!/usr/bin/env python3
"""
Deploy MLflow test infrastructure on an OpenShift AI cluster.

Deployed for one isolated test-matrix cell and torn down by
cleanup_test_resources before the next cell:

1. Per-test PostgreSQL and MinIO credentials
2. MinIO (S3-compatible artifact storage) — PVC, Deployment, Service
3. PostgreSQL (metadata backend store) — PVC, Deployment, Service
   (PVC is sized for the current cell's pg_size_gb)
4. MinIO bucket creation job
5. MLflow database, S3, and artifact connection Secrets
6. MLflow CR (deploys the MLflow server via the operator)
7. Per-project MLflowConfig and RBAC RoleBinding

Seeding PostgreSQL to the current pg_size_gb cell target is handled separately
by the seed_postgres toolbox. It creates its own experiment (one per seeded
trace, via SQL), and each k6 write job creates its own experiment pool during
setup.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import oc, oc_apply
from projects.mlflow.library.credentials import (
    apply_mlflow_credentials,
    ensure_exporter_role,
    ensure_storage_credentials,
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
MINIO_PVC_GI = 20

EXPECTED_POSTGRESQL_MAJOR = 18


def postgres_pvc_gi(pg_size_gb: int) -> int:
    """PVC size with headroom for indexes, WAL, and system catalogs."""
    if pg_size_gb <= 0:
        return 10
    return max(20, int(pg_size_gb * 1.5) + 10)


@entrypoint
def run(
    *,
    namespace: str = "mlflow-perf-testing",
    operator_namespace: str = "redhat-ods-applications",
    max_pg_size_gb: int = 0,
    storage_class_name: str,
    node_selectors: str | dict[str, dict[str, str]] | None = None,
    pvc_selectors: str | dict[str, dict[str, str]] | None = None,
    mlflow_replicas: int = 1,
    mlflow_workers: int = 4,
    mlflow_image: str = "quay.io/opendatahub/mlflow:odh-stable",
    mlflow_image_pull_policy: str = "Always",
    mlflow_pod_annotations: str | dict[str, str] | None = None,
) -> int:
    """
    Deploy one MLflow test stack with PostgreSQL and MinIO.

    Args:
        namespace: Namespace hosting PostgreSQL and MinIO
        operator_namespace: Namespace hosting the MLflow server
        max_pg_size_gb: Target database size used to size the PVC, or 0 for the default
        storage_class_name: StorageClass used by the MinIO and PostgreSQL PVCs.
        node_selectors: JSON object mapping components to node selectors.
        pvc_selectors: JSON object mapping claims to selectors for local PVs.
        mlflow_replicas: Number of MLflow server replicas in this test cell.
        mlflow_workers: Gunicorn workers per MLflow server replica.
        mlflow_image: Container image for the MLflow server.
        mlflow_image_pull_policy: Kubernetes image pull policy for the server.
        mlflow_pod_annotations: JSON object of annotations merged into the MLflow CR.
    """
    if isinstance(node_selectors, dict):
        node_selectors = json.dumps(node_selectors)
    if isinstance(pvc_selectors, dict):
        pvc_selectors = json.dumps(pvc_selectors)
    if isinstance(mlflow_pod_annotations, dict):
        mlflow_pod_annotations = json.dumps(mlflow_pod_annotations)
    execute_tasks(locals())
    return 0


@task
def validate_storage_class(args, ctx):
    """Check the configured StorageClass before creating PVCs."""
    storage_class_name = str(args.storage_class_name).strip()
    if not storage_class_name:
        raise ValueError("storage_class_name must be a non-empty string")

    result = oc(
        "get",
        "storageclass",
        storage_class_name,
        "-o",
        "json",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"StorageClass '{storage_class_name}' is not available in the cluster")

    storage_class = json.loads(result.stdout)
    provisioner = storage_class.get("provisioner", "")
    binding_mode = storage_class.get("volumeBindingMode", "")
    if provisioner == "topolvm.io" and binding_mode != "WaitForFirstConsumer":
        raise RuntimeError(
            f"TopoLVM StorageClass '{storage_class_name}' must use "
            "volumeBindingMode: WaitForFirstConsumer for node-local volume placement"
        )

    node_selectors = json.loads(args.node_selectors or "{}")
    if not isinstance(node_selectors, dict):
        raise ValueError("node_selectors must be a mapping of components to selectors")
    components = {"minio", "postgresql", "mlflow", "k6", "minio_bucket"}
    unknown = set(node_selectors) - components
    if unknown:
        raise ValueError("Unknown node selector component(s): " + ", ".join(sorted(unknown)))
    normalized_selectors: dict[str, dict[str, str]] = {}
    for component in components:
        selector = node_selectors.get(component, {})
        if not isinstance(selector, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in selector.items()
        ):
            raise ValueError(
                f"node_selectors.{component} must map non-empty label names "
                "to non-empty string values"
            )
        normalized_selectors[component] = selector

    pvc_selectors = json.loads(args.pvc_selectors or "{}")
    if not isinstance(pvc_selectors, dict):
        raise ValueError("pvc_selectors must map claim names to label selectors")
    normalized_pvc_selectors: dict[str, dict[str, str]] = {}
    for claim_name, selector in pvc_selectors.items():
        if claim_name not in {"minio-pvc", "postgresql-pvc"}:
            raise ValueError(f"Unsupported PVC selector claim: {claim_name}")
        if (
            not isinstance(selector, dict)
            or not selector
            or any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value.strip()
                for key, value in selector.items()
            )
        ):
            raise ValueError(
                f"pvc_selectors.{claim_name} must map non-empty label names "
                "to non-empty string values"
            )
        normalized_pvc_selectors[claim_name] = selector

    ctx.storage_class_name = storage_class_name
    ctx.node_selectors = normalized_selectors
    ctx.pvc_selectors = normalized_pvc_selectors
    return (
        f"Using StorageClass {storage_class_name} "
        f"(provisioner={provisioner}, volumeBindingMode={binding_mode or 'unset'})"
    )


@task
def create_storage_credentials(args, ctx):
    """Create or reuse the per-test MinIO, PostgreSQL, and exporter Secrets."""
    ensure_storage_credentials(args.namespace)
    return f"Storage credentials ready in {args.namespace}"


@task
def deploy_minio(args, ctx):
    """Deploy MinIO (PVC + Deployment + Service)"""
    docs = _render_docs(args, "01-minio.yaml")
    _configure_storage_docs(
        docs,
        storage_class_name=ctx.storage_class_name,
        node_selector=ctx.node_selectors["minio"],
        pvc_selector=ctx.pvc_selectors.get("minio-pvc"),
    )
    _apply_docs(args.artifact_dir / "minio.yaml", docs)
    oc(
        "wait",
        "--for=condition=available",
        "deployment/minio",
        "-n",
        args.namespace,
        "--timeout=120s",
    )
    return "MinIO deployed and ready"


@task
def deploy_postgresql(args, ctx):
    """Deploy PostgreSQL with the credentials Secret created earlier."""
    _apply_postgresql_manifest(
        args.artifact_dir / "postgresql.yaml",
        _render_docs(args, "02-postgresql.yaml"),
        int(args.max_pg_size_gb or 0),
        storage_class_name=ctx.storage_class_name,
        node_selector=ctx.node_selectors["postgresql"],
        pvc_selector=ctx.pvc_selectors.get("postgresql-pvc"),
    )
    max_pg_size_gb = int(args.max_pg_size_gb or 0)
    wait_timeout_seconds = 600 if max_pg_size_gb > 0 else 120
    oc(
        "wait",
        "--for=condition=available",
        "deployment/postgresql",
        "-n",
        args.namespace,
        f"--timeout={wait_timeout_seconds}s",
        timeout_seconds=wait_timeout_seconds + 30,
    )
    return f"PostgreSQL Deployment available (PVC {postgres_pvc_gi(max_pg_size_gb)}Gi)"


@retry(attempts=30, delay=5, backoff=1.0, retry_on_exceptions=True)
@task
def wait_postgresql_ready(args, ctx):
    """Wait until PostgreSQL accepts SQL connections and capture its version."""
    version_result = oc(
        "exec",
        "-n",
        args.namespace,
        "deployment/postgresql",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "mlflow",
        "-At",
        "-c",
        "SHOW server_version_num;",
    )
    try:
        server_version_num = int(version_result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Could not read PostgreSQL server_version_num: {version_result.stdout!r}"
        ) from exc

    ctx.postgresql_server_version_num = server_version_num
    return "PostgreSQL accepts SQL connections"


@task
def verify_postgresql_version(args, ctx):
    """Fail clearly if the test database is not the expected PostgreSQL major."""
    server_version_num = ctx.postgresql_server_version_num
    actual_major = server_version_num // 10000
    if actual_major != EXPECTED_POSTGRESQL_MAJOR:
        raise RuntimeError(
            f"Expected PostgreSQL {EXPECTED_POSTGRESQL_MAJOR}, "
            f"but the test database is PostgreSQL {actual_major} "
            f"(server_version_num={server_version_num})"
        )
    logger.info("Verified test database is PostgreSQL %d", actual_major)
    return f"Verified test database is PostgreSQL {actual_major}"


@retry(attempts=6, delay=5, backoff=1.0, retry_on_exceptions=True)
@task
def enable_pg_stat_statements(args, ctx):
    """Enable pg_stat_statements extension for query performance tracking."""
    oc(
        "exec",
        "-n",
        args.namespace,
        "deployment/postgresql",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "mlflow",
        "-c",
        "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;",
    )
    return "pg_stat_statements extension enabled"


@retry(attempts=6, delay=5, backoff=1.0, retry_on_exceptions=True)
@task
def create_pg_exporter_role(args, ctx):
    """Create or restore the least-privilege PostgreSQL exporter login."""
    ensure_exporter_role(args.namespace)
    return "pgexporter role ready"


@task
def deploy_postgresql_servicemonitor(args, ctx):
    """Deploy ServiceMonitor for PostgreSQL metrics scraping by Prometheus."""
    _apply_docs(
        args.artifact_dir / "postgresql-servicemonitor.yaml",
        _render_docs(args, "09-postgresql-servicemonitor.yaml"),
    )
    return "PostgreSQL ServiceMonitor created"


@retry(attempts=6, delay=5, backoff=1.0, retry_on_exceptions=True)
@task
def create_minio_bucket(args, ctx):
    """Create the mlflow-artifacts bucket via a Job"""
    docs = _render_docs(args, "07-minio-create-bucket.yaml")
    for doc in docs:
        if not doc or doc.get("kind") != "Job":
            continue
        pod_spec = doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
        _set_node_selector(pod_spec, ctx.node_selectors["minio_bucket"])
    _apply_docs(args.artifact_dir / "minio-create-bucket.yaml", docs)
    oc(
        "wait",
        "--for=condition=complete",
        "job/minio-create-bucket",
        "-n",
        args.namespace,
        "--timeout=60s",
    )
    return "MinIO bucket 'mlflow-artifacts' created"


@task
def create_mlflow_secrets(args, ctx):
    """Deploy the Secret references used by MLflow and its artifact connection."""
    apply_mlflow_credentials(args.namespace, args.operator_namespace)
    return f"MLflow secrets created in {args.operator_namespace}"


@task
def create_mlflow_cr(args, ctx):
    """Create the cell-configured MLflow CR and trigger server deployment."""
    replicas = int(args.mlflow_replicas)
    workers = int(args.mlflow_workers)
    if replicas <= 0 or workers <= 0:
        raise ValueError("mlflow_replicas and mlflow_workers must be positive")

    (manifest,) = _render_docs(args, "05-mlflow-cr.yaml")
    if not isinstance(manifest, dict) or manifest.get("kind") != "MLflow":
        raise ValueError("05-mlflow-cr.yaml must contain an MLflow custom resource")
    spec = manifest.setdefault("spec", {})
    spec["replicas"] = replicas
    spec["workers"] = workers
    if not isinstance(args.mlflow_image, str) or not args.mlflow_image.strip():
        raise ValueError("mlflow_image must be a non-empty string")
    if not isinstance(args.mlflow_image_pull_policy, str) or args.mlflow_image_pull_policy not in {
        "Always",
        "IfNotPresent",
        "Never",
    }:
        raise ValueError("mlflow_image_pull_policy must be Always, IfNotPresent, or Never")
    image_config = spec.setdefault("image", {})
    if not isinstance(image_config, dict):
        raise ValueError("MLflow CR spec.image must be a mapping")
    image_config["image"] = args.mlflow_image.strip()
    image_config["imagePullPolicy"] = args.mlflow_image_pull_policy
    annotations = json.loads(args.mlflow_pod_annotations or "{}")
    if not isinstance(annotations, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in annotations.items()
    ):
        raise ValueError("mlflow_pod_annotations must map non-empty strings to strings")
    if annotations:
        pod_annotations = spec.setdefault("podAnnotations", {})
        if not isinstance(pod_annotations, dict):
            raise ValueError("MLflow CR spec.podAnnotations must be a mapping")
        pod_annotations.update(annotations)
    oc_apply(args.artifact_dir / "mlflow-cr.yaml", manifest)
    return (
        f"MLflow CR created (replicas={replicas}, workers={workers}, "
        f"image={args.mlflow_image.strip()})"
    )


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_mlflow_server_ready(args, ctx):
    """Wait until all requested MLflow replicas are available."""
    result = oc(
        "get",
        "deployment",
        "mlflow",
        "-n",
        args.operator_namespace,
        "-o",
        "jsonpath={.status.availableReplicas}",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return (False, "Waiting for MLflow Deployment availability")

    available = int(result.stdout.strip())
    requested = int(args.mlflow_replicas)
    if available >= requested:
        return f"MLflow server ready ({available}/{requested} replicas available)"
    return (False, f"MLflow server replicas available: {available}/{requested}")


@task
def patch_mlflow_node_selector(args, ctx):
    """Apply the configured selector to the operator-managed MLflow Deployment.

    The MLflow CR does not expose a nodeSelector field, so we patch the
    Deployment directly after the operator has created it.
    """
    selector = ctx.node_selectors["mlflow"]
    patch = json.dumps(
        {
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": selector or None,
                    }
                }
            }
        }
    )
    oc(
        "patch",
        "deployment",
        "mlflow",
        "-n",
        args.operator_namespace,
        "--type=merge",
        f"-p={patch}",
    )
    oc(
        "rollout",
        "status",
        "deployment/mlflow",
        "-n",
        args.operator_namespace,
        "--timeout=120s",
    )
    return "MLflow deployment patched with configured node selector and rolled out"


@task
def deploy_mlflow_podmonitor(args, ctx):
    """Deploy PodMonitor for MLflow metrics scraping by Prometheus."""
    _apply_docs(
        args.artifact_dir / "mlflow-podmonitor.yaml",
        _render_docs(args, "10-mlflow-servicemonitor.yaml"),
    )
    return "MLflow PodMonitor created"


@task
def configure_per_project(args, ctx):
    """Apply the MLflowConfig that references the artifact connection Secret."""
    _apply_docs(
        args.artifact_dir / "mlflowconfig-per-project.yaml",
        _render_docs(args, "06-mlflowconfig-per-project.yaml"),
    )
    return f"Per-project MLflowConfig applied in {args.namespace}"


@task
def create_rbac(args, ctx):
    """Create RBAC RoleBinding for service account access"""
    _apply_docs(args.artifact_dir / "mlflow-rbac.yaml", _render_docs(args, "08-mlflow-rbac.yaml"))
    return f"RBAC RoleBinding created in {args.namespace}"


@always
@task
def capture_infrastructure_state(args, ctx):
    """Capture infrastructure state for diagnostics"""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    _capture_to_file(
        artifacts_dir / "pods-perf-testing.txt",
        "get",
        "pods",
        "-n",
        args.namespace,
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "pods-mlflow-server.txt",
        "get",
        "pods",
        "-n",
        args.operator_namespace,
        "-l",
        "app=mlflow",
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "mlflow-cr.yaml",
        "get",
        "mlflow",
        "mlflow",
        "-n",
        args.operator_namespace,
        "-o",
        "yaml",
    )
    _capture_to_file(
        artifacts_dir / "services-perf-testing.txt",
        "get",
        "svc",
        "-n",
        args.namespace,
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "pvcs-perf-testing.txt",
        "get",
        "pvc",
        "-n",
        args.namespace,
        "-o",
        "wide",
    )
    _capture_to_file(
        artifacts_dir / "events-perf-testing.txt",
        "get",
        "events",
        "-n",
        args.namespace,
        "--sort-by=.lastTimestamp",
    )

    return "Captured infrastructure state"


def _apply_postgresql_manifest(
    artifact_path: Path,
    docs: list[dict],
    pg_size_gb: int,
    *,
    storage_class_name: str,
    node_selector: dict[str, str],
    pvc_selector: dict[str, str] | None = None,
) -> None:
    """Apply the PostgreSQL manifest, expanding the PVC when seeding."""
    pvc_gi = postgres_pvc_gi(pg_size_gb)
    for doc in docs:
        if doc and doc.get("kind") == "PersistentVolumeClaim":
            doc["spec"]["resources"]["requests"]["storage"] = f"{pvc_gi}Gi"
            logger.info("PostgreSQL PVC size set to %sGi", pvc_gi)
    _configure_storage_docs(
        docs,
        storage_class_name=storage_class_name,
        node_selector=node_selector,
        pvc_selector=pvc_selector,
    )

    _apply_docs(artifact_path, docs)


def _render_docs(args, filename: str) -> list[dict]:
    rendered = render_template(
        f"{filename}.j2",
        {
            "namespace": args.namespace,
            "operator_namespace": args.operator_namespace,
            "minio_pvc_gi": MINIO_PVC_GI,
        },
        templates_dir=TEMPLATES_DIR,
    )
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _configure_storage_docs(
    docs: list[dict],
    *,
    storage_class_name: str,
    node_selector: dict[str, str],
    pvc_selector: dict[str, str] | None = None,
) -> None:
    """Set PVC storage and apply its runtime-selected placement constraints."""
    for doc in docs:
        if not doc:
            continue
        if doc.get("kind") == "PersistentVolumeClaim":
            pvc_spec = doc.setdefault("spec", {})
            pvc_spec["storageClassName"] = storage_class_name
            if pvc_selector:
                pvc_spec["selector"] = {"matchLabels": pvc_selector}
            continue
        if doc.get("kind") != "Deployment":
            continue

        pod_spec = doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
        _set_node_selector(pod_spec, node_selector)


def _set_node_selector(pod_spec: dict, node_selector: dict[str, str]) -> None:
    """Set or clear a pod's nodeSelector from component configuration."""
    if node_selector:
        pod_spec["nodeSelector"] = node_selector
    else:
        pod_spec.pop("nodeSelector", None)


def _apply_docs(artifact_path: Path, docs: list[dict]) -> None:
    """Save and apply non-Secret resources."""
    if any(doc["kind"] == "Secret" for doc in docs):
        raise ValueError("Secret manifests must be applied through the credential helpers")
    artifact_path.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
    oc("apply", "-f", str(artifact_path))


def _capture_to_file(path: Path, *oc_args: str) -> None:
    oc(*oc_args, check=False, log_stdout=False, stdout_dest=path)


if __name__ == "__main__":
    run.main()
