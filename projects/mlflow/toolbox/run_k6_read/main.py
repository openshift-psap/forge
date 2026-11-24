"""Run a single-operation MLflow read benchmark with optional k6 warmup."""

from __future__ import annotations

import json
from pathlib import Path

from projects.core.dsl import always, entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc, oc_apply
from projects.mlflow.toolbox.k6 import (
    K6Results,
    phase_hooks,
    run_job,
    selector_argument,
    validate_tracking_uri,
    with_suffix,
)

WARMUP_TRACE_SELECTION_SEED = 104729
MEASUREMENT_TRACE_SELECTION_SEED = 130363


@entrypoint
def run(
    *,
    job_name: str,
    namespace: str,
    tracking_uri: str,
    users: int,
    tenant_count: int = 1,
    users_per_tenant: int | None = None,
    duration_seconds: int,
    warmup_duration_seconds: int = 0,
    warmup_job_name: str | None = None,
    operation: str,
    trace_data_path: str | Path,
    image: str,
    storage_class_name: str,
    node_selector: str | dict[str, str] | None = None,
    pvc_selector: str | dict[str, str] | None = None,
    search_filter: str = "trace.status = 'OK'",
    search_order_by: str = "timestamp_ms DESC",
    search_max_results: int = 100,
) -> K6Results:
    """Run a k6 trace read benchmark with optional warmup.

    Args:
        operation: MLflow API operation, get_trace or search_traces
        trace_data_path: Path to the JSON file containing the tenant trace pools
        storage_class_name: StorageClass for the temporary trace data PVC
        node_selector: JSON object of node labels to match
        pvc_selector: JSON object of persistent volume labels to match
    """
    validate_tracking_uri(tracking_uri)
    trace_data_path = str(Path(trace_data_path).resolve())
    node_selector = selector_argument(node_selector)
    pvc_selector = selector_argument(pvc_selector)
    if isinstance(users_per_tenant, str):
        users_per_tenant = int(users_per_tenant)
    return execute_tasks(locals())._results


@task
def validate_inputs(args, ctx):
    """Validate the requested workload and its local trace data."""
    if args.operation not in {"get_trace", "search_traces"}:
        raise ValueError(f"Unsupported read operation: {args.operation}")
    if args.warmup_duration_seconds < 0:
        raise ValueError("warmup_duration_seconds must be non-negative")
    if args.duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    tenant_count = args.tenant_count
    users = args.users
    users_per_tenant = args.users_per_tenant
    if tenant_count < 1 or users < 1:
        raise ValueError("tenant_count and users must be positive")
    if users_per_tenant is None:
        if users % tenant_count:
            raise ValueError("users must divide evenly across tenant_count")
        users_per_tenant = users // tenant_count
    if users_per_tenant < 1 or users != tenant_count * users_per_tenant:
        raise ValueError("users must equal tenant_count * users_per_tenant")
    trace_data_path = Path(args.trace_data_path)
    if not trace_data_path.is_file():
        raise FileNotFoundError(f"Trace data file not found: {trace_data_path}")

    trace_data = json.loads(trace_data_path.read_text(encoding="utf-8"))
    tenants = trace_data.get("tenants") if isinstance(trace_data, dict) else None
    if not isinstance(tenants, list) or len(tenants) != tenant_count:
        raise ValueError(
            f"trace data must contain one tenant pool for each of {tenant_count} tenants"
        )
    for tenant in tenants:
        if (
            not isinstance(tenant, dict)
            or not tenant.get("workspace")
            or not tenant.get("trace_ids")
        ):
            raise ValueError("each tenant trace pool must have a workspace and trace IDs")
        if args.operation == "search_traces" and not tenant.get("experiment_ids"):
            raise ValueError("each tenant search pool must have experiment IDs")

    ctx.users_per_tenant = users_per_tenant
    ctx.node_selector = json.loads(args.node_selector or "{}")
    ctx.pvc_selector = json.loads(args.pvc_selector or "{}")
    ctx.trace_pvc = with_suffix(args.job_name, "-traces")
    ctx.staging_pod = with_suffix(args.job_name, "-trace-stager")
    ctx.warmup_job = args.warmup_job_name or with_suffix(args.job_name, "-warmup")
    ctx._jobs = []


@task
def stage_trace_data(args, ctx):
    """Create the temporary PVC and upload the k6 script and trace pools."""
    namespace = args.namespace
    trace_pvc = ctx.trace_pvc
    staging_pod = ctx.staging_pod
    pvc_manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": trace_pvc,
            "namespace": namespace,
            "labels": {
                "forge.openshift.io/project": "mlflow",
                "forge.openshift.io/read-data": "k6",
            },
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": args.storage_class_name,
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }
    if ctx.pvc_selector:
        pvc_manifest["spec"]["selector"] = {"matchLabels": ctx.pvc_selector}
    staging_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": staging_pod,
            "namespace": namespace,
            "labels": {"forge.openshift.io/project": "mlflow"},
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": ctx.node_selector,
            "containers": [
                {
                    "name": "stager",
                    "image": args.image,
                    "command": ["/bin/sh", "-c", "sleep 3600"],
                    "volumeMounts": [{"name": "trace-data", "mountPath": "/trace-data"}],
                }
            ],
            "volumes": [
                {
                    "name": "trace-data",
                    "persistentVolumeClaim": {"claimName": trace_pvc},
                }
            ],
        },
    }

    ctx._staging_started = True
    oc_apply(args.artifact_dir / "trace-pvc.yaml", pvc_manifest)
    oc_apply(args.artifact_dir / "trace-stager.yaml", staging_manifest)
    oc(
        "wait",
        "--for=jsonpath={.status.phase}=Bound",
        f"pvc/{trace_pvc}",
        "-n",
        namespace,
        "--timeout=180s",
    )
    oc("wait", "--for=condition=Ready", f"pod/{staging_pod}", "-n", namespace, "--timeout=180s")
    script_path = Path(__file__).resolve().parent / "scripts" / "mlflow_trace_read_test.js"
    destination = f"{namespace}/{staging_pod}:/trace-data"
    oc("cp", str(script_path), f"{destination}/{script_path.name}")
    oc("cp", args.trace_data_path, f"{destination}/trace_data.json")
    oc(
        "delete",
        "pod",
        staging_pod,
        "-n",
        namespace,
        "--ignore-not-found=true",
        "--wait=true",
        "--timeout=180s",
    )


@task
def run_warmup(args, ctx):
    """Run the read warmup when a warmup duration was requested."""
    if not args.warmup_duration_seconds:
        return
    manifest = _job_manifest(
        args,
        ctx,
        name=ctx.warmup_job,
        duration=args.warmup_duration_seconds,
        selection_seed=WARMUP_TRACE_SELECTION_SEED,
    )
    ctx._jobs.append(ctx.warmup_job)
    run_job(args.artifact_dir, manifest, "warmup", on_ready=phase_hooks.get().warmup)


@task
def run_measurement(args, ctx):
    """Run the measured read workload and save its k6 output."""
    manifest = _job_manifest(
        args,
        ctx,
        name=args.job_name,
        duration=args.duration_seconds,
        selection_seed=MEASUREMENT_TRACE_SELECTION_SEED,
    )
    ctx._jobs.append(args.job_name)
    logs = run_job(
        args.artifact_dir,
        manifest,
        "measurement",
        on_ready=phase_hooks.get().measurement,
    )
    ctx._results = K6Results.from_job_log(logs)


@always
@task
def cleanup(args, ctx):
    """Delete benchmark Jobs and temporary read data resources."""
    if not getattr(ctx, "_staging_started", False):
        return
    for name in ctx._jobs:
        oc(
            "delete",
            "job",
            name,
            "-n",
            args.namespace,
            "--ignore-not-found=true",
            "--cascade=foreground",
            "--wait=true",
            "--timeout=180s",
            check=False,
        )
    oc(
        "delete",
        "pod",
        ctx.staging_pod,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        check=False,
    )
    oc(
        "delete",
        "pvc",
        ctx.trace_pvc,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        "--wait=true",
        "--timeout=180s",
        check=False,
    )


def _job_manifest(args, ctx, *, name: str, duration: int, selection_seed: int) -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": args.namespace,
            "labels": {"forge.openshift.io/project": "mlflow"},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": "mlflow-k6-read", "job": name}},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": ctx.node_selector,
                    "containers": [
                        {
                            "name": "k6",
                            "image": args.image,
                            "command": ["k6", "run"],
                            "args": [
                                "-e",
                                f"MLFLOW_TRACKING_URI={args.tracking_uri}",
                                "-e",
                                f"USERS={args.users}",
                                "-e",
                                f"TENANT_COUNT={args.tenant_count}",
                                "-e",
                                f"USERS_PER_TENANT={ctx.users_per_tenant}",
                                "-e",
                                f"DURATION={duration}s",
                                "-e",
                                f"READ_OPERATION={args.operation}",
                                "-e",
                                f"SEARCH_FILTER={args.search_filter}",
                                "-e",
                                f"SEARCH_ORDER_BY={args.search_order_by}",
                                "-e",
                                f"SEARCH_MAX_RESULTS={args.search_max_results}",
                                "-e",
                                f"TRACE_SELECTION_SEED={selection_seed}",
                                "/trace-data/mlflow_trace_read_test.js",
                                "--insecure-skip-tls-verify",
                            ],
                            "volumeMounts": [
                                {
                                    "name": "trace-data",
                                    "mountPath": "/trace-data",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "trace-data",
                            "persistentVolumeClaim": {
                                "claimName": ctx.trace_pvc,
                                "readOnly": True,
                            },
                        }
                    ],
                },
            },
        },
    }


if __name__ == "__main__":
    run.main()
