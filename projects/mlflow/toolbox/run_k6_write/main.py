"""Run a raw MLflow write workload in one k6 Job.

The JavaScript workload creates its experiment pool in k6 setup(), before any
VU starts. Scripts are mounted from a ConfigMap, matching the archived
mlflow-scale runner; no data PVC is needed.
"""

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

_SCRIPT_NAMES = {
    "trace_meta_write": "trace_meta_write.js",
    "span_cold_start": "span_cold_start.js",
    "span_warm_start": "span_warm_start.js",
}


@entrypoint
def run(
    *,
    job_name: str,
    namespace: str,
    tracking_uri: str,
    users: int,
    tenant_count: int,
    users_per_tenant: int,
    duration_seconds: int,
    mode: str,
    image: str,
    span_size_bytes: int,
    spans_per_trace: int,
    traces_per_experiment: int,
    experiment_prefix: str,
    otlp_path: str | None = None,
    warmup_duration_seconds: int = 0,
    ramp_up_warmup: bool = False,
    warmup_job_name: str | None = None,
    node_selector: str | dict[str, str] | None = None,
) -> K6Results:
    """Run a k6 trace write benchmark with optional warmup.

    Args:
        mode: Workload script to run, trace_meta_write, span_cold_start or span_warm_start
        ramp_up_warmup: Ramp up virtual users during warmup
        node_selector: JSON object of node labels to match
    """
    validate_tracking_uri(tracking_uri)
    node_selector = selector_argument(node_selector)
    return execute_tasks(locals())._results


@task
def validate_inputs(args, ctx):
    """Validate the workload and locate its bundled k6 scripts."""
    if args.warmup_duration_seconds < 0:
        raise ValueError("warmup_duration_seconds must be non-negative")
    if args.duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if args.ramp_up_warmup and args.warmup_duration_seconds == 0:
        raise ValueError("ramp_up_warmup requires a positive warmup_duration_seconds")
    if args.tenant_count < 1 or args.users_per_tenant < 1:
        raise ValueError("tenant_count and users_per_tenant must be positive")
    if args.users != args.tenant_count * args.users_per_tenant:
        raise ValueError("users must equal tenant_count * users_per_tenant")
    try:
        ctx.script_name = _SCRIPT_NAMES[args.mode]
    except KeyError as exc:
        raise ValueError(f"Unsupported k6 write mode: {args.mode}") from exc

    scripts_dir = Path(__file__).resolve().parent / "scripts"
    ctx.script_path = scripts_dir / ctx.script_name
    ctx.helper_path = scripts_dir / "_common.js"
    if not ctx.script_path.is_file() or not ctx.helper_path.is_file():
        raise FileNotFoundError(f"k6 write workload files missing under {scripts_dir}")

    ctx.node_selector = json.loads(args.node_selector or "{}")
    ctx.configmap_name = with_suffix(args.job_name, "-write-scripts")
    ctx.warmup_job = args.warmup_job_name or with_suffix(args.job_name, "-warmup")
    ctx._jobs = []


@task
def stage_scripts(args, ctx):
    """Mount the selected script and its helper through a ConfigMap."""
    configmap_manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ctx.configmap_name,
            "namespace": args.namespace,
            "labels": {
                "forge.openshift.io/project": "mlflow",
                "forge.openshift.io/write-data": "k6",
            },
        },
        "data": {
            ctx.script_name: ctx.script_path.read_text(encoding="utf-8"),
            "_common.js": ctx.helper_path.read_text(encoding="utf-8"),
        },
    }

    ctx._staging_started = True
    oc_apply(args.artifact_dir / "write-scripts.yaml", configmap_manifest)


@task
def run_warmup(args, ctx):
    """Run the write warmup when a warmup duration was requested."""
    if not args.warmup_duration_seconds:
        return
    manifest = _job_manifest(
        args,
        ctx,
        job_name=ctx.warmup_job,
        duration=args.warmup_duration_seconds,
        ramp_up=args.ramp_up_warmup,
    )
    ctx._jobs.append(ctx.warmup_job)
    run_job(
        args.artifact_dir, manifest, "warmup", on_ready=phase_hooks.get().warmup, run_setup=True
    )


@task
def run_measurement(args, ctx):
    """Run the measured write workload and save its k6 output."""
    manifest = _job_manifest(args, ctx, job_name=args.job_name, duration=args.duration_seconds)
    ctx._jobs.append(args.job_name)
    logs = run_job(
        args.artifact_dir,
        manifest,
        "measurement",
        on_ready=phase_hooks.get().measurement,
        run_setup=True,
    )
    ctx._results = K6Results.from_job_log(logs)


@always
@task
def cleanup(args, ctx):
    """Delete benchmark Jobs and the temporary script ConfigMap."""
    if not getattr(ctx, "_staging_started", False):
        return
    for name in ctx._jobs:
        oc("delete", "job", name, "-n", args.namespace, "--ignore-not-found=true", check=False)
    oc(
        "delete",
        "configmap",
        ctx.configmap_name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        check=False,
    )


def _job_manifest(args, ctx, *, job_name: str, duration: int, ramp_up: bool = False) -> dict:
    env = [
        {"name": "MLFLOW_TRACKING_URI", "value": str(args.tracking_uri)},
        {"name": "USERS", "value": str(args.users)},
        {"name": "TENANT_COUNT", "value": str(args.tenant_count)},
        {"name": "USERS_PER_TENANT", "value": str(args.users_per_tenant)},
        {"name": "DURATION", "value": f"{duration}s"},
        {"name": "SPAN_SIZE_BYTES", "value": str(args.span_size_bytes)},
        {"name": "SPANS_PER_TRACE", "value": str(args.spans_per_trace)},
        {"name": "TRACES_PER_EXPERIMENT", "value": str(args.traces_per_experiment)},
        {"name": "EXPERIMENT_PREFIX", "value": str(args.experiment_prefix)},
        {
            "name": "WARMUP_RAMP_UP",
            "value": str(ramp_up).lower(),
        },
    ]
    if args.otlp_path:
        env.append({"name": "MLFLOW_OTLP_PATH", "value": str(args.otlp_path)})
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": args.namespace,
            "labels": {
                "forge.openshift.io/project": "mlflow",
                "forge.openshift.io/write-data": "k6",
            },
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": "mlflow-k6-write", "job": job_name}},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": ctx.node_selector,
                    "containers": [
                        {
                            "name": "k6",
                            "image": args.image,
                            "command": ["k6", "run"],
                            "args": [
                                f"/write-data/{ctx.script_name}",
                                "--insecure-skip-tls-verify",
                            ],
                            "env": env,
                            "volumeMounts": [
                                {"name": "write-data", "mountPath": "/write-data", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [{"name": "write-data", "configMap": {"name": ctx.configmap_name}}],
                },
            },
        },
    }


if __name__ == "__main__":
    run.main()
