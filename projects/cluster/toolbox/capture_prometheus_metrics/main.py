#!/usr/bin/env python3

"""
Capture Prometheus Metrics via kube-burner

Spawns a kube-burner pod in openshift-monitoring that runs
``kube-burner index`` to execute PromQL range queries against
Thanos Querier. Results are saved as JSON files per metric.

Can be run standalone:
    ./bin/run_toolbox cluster capture_prometheus_metrics \\
        --metric-profiles /path/to/profile.yaml \\
        --start-time "2026-07-26T10:00:00+00:00" \\
        --end-time "2026-07-26T10:20:00+00:00"
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import yaml

from projects.core.dsl import (  # noqa: E501
    always,
    entrypoint,
    execute_tasks,
    retry,
    shell,
    task,
    template,
)
from projects.core.dsl.utils.k8s import best_effort_oc, oc, oc_cp_from_pod

logger = logging.getLogger("DSL")

KUBE_BURNER_IMAGE = "quay.io/kube-burner/kube-burner:v2.8.6"
SIDECAR_IMAGE = "registry.access.redhat.com/ubi9:9.5"

PROM_NAMESPACE = "openshift-monitoring"
PROM_URL = "https://thanos-querier.openshift-monitoring.svc:9091"
PROM_SERVICE_ACCOUNT = "prometheus-k8s"

CONFIGMAP_MOUNT = "/etc/kube-burner"
RESULTS_DIR = "/tmp/results"
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


@entrypoint
def run(
    metric_profiles: list[str],
    start_time: datetime,
    end_time: datetime,
    output_dir: str | Path | None = None,
    *,
    step_seconds: int = 15,
    variables: dict[str, str] | None = None,
    prom_namespace: str | None = None,
    prom_url: str | None = None,
    prom_service_account: str | None = None,
    kube_burner_image: str | None = None,
) -> Path:
    """
    Query Prometheus from inside the cluster using kube-burner and save results as JSON.

    Creates a temporary kube-burner pod, runs ``kube-burner index`` with
    the given metric profiles, and copies the result JSON files to the
    output directory.

    Args:
        metric_profiles: Paths to kube-burner metric profile YAML files.
        start_time: Start of the query window (UTC).
        end_time: End of the query window (UTC).
        step_seconds: Query resolution step in seconds.
        variables: Environment variables for Go template rendering in metric profiles.
        output_dir: Directory to write results into (default: artifact_dir/prometheus_metrics).
        prom_namespace: Override the namespace for the capture pod.
        prom_url: Override the Prometheus/Thanos URL.
        prom_service_account: Override the service account.
        kube_burner_image: Override the kube-burner container image.
    """
    ctx = execute_tasks(locals())
    return ctx.output_dir


def _parse_time(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@task
def validate_parameters(args, ctx):
    """Validate and normalize input parameters."""
    ctx.start_time = _parse_time(args.start_time)
    ctx.end_time = _parse_time(args.end_time)

    if ctx.end_time <= ctx.start_time:
        raise ValueError("end_time must be after start_time")

    ctx.prom_namespace = args.prom_namespace or PROM_NAMESPACE
    ctx.prom_url = args.prom_url or PROM_URL
    ctx.prom_service_account = args.prom_service_account or PROM_SERVICE_ACCOUNT
    ctx.kube_burner_image = args.kube_burner_image or KUBE_BURNER_IMAGE
    ctx.variables = args.variables or {}

    if args.output_dir is not None:
        ctx.output_dir = Path(args.output_dir)
    else:
        ctx.output_dir = args.artifact_dir / "prometheus_metrics"
    ctx.output_dir.mkdir(parents=True, exist_ok=True)

    ctx.metric_profiles = []
    for profile_path in args.metric_profiles:
        p = Path(profile_path)
        if not p.exists():
            raise FileNotFoundError(f"metric profile not found: {p}")
        ctx.metric_profiles.append(p)

    if not ctx.metric_profiles:
        raise ValueError("No metric profiles specified")

    ctx.start_epoch = int(ctx.start_time.timestamp())
    ctx.end_epoch = int(ctx.end_time.timestamp())
    ctx.pod_name = f"kube-burner-capture-{ctx.start_epoch}"
    ctx.configmap_name = f"kube-burner-profiles-{ctx.start_epoch}"

    duration_min = (ctx.end_time - ctx.start_time).total_seconds() / 60
    profile_names = [p.stem for p in ctx.metric_profiles]
    return (
        f"Profiles: {', '.join(profile_names)}, window: "
        f"{ctx.start_time:%H:%M:%S} -> {ctx.end_time:%H:%M:%S} ({duration_min:.1f} min)"
    )


@task
def create_configmap(args, ctx):
    """Create a ConfigMap with the kube-burner endpoint config and metric profiles."""
    best_effort_oc(
        "delete",
        "configmap",
        ctx.configmap_name,
        "-n",
        ctx.prom_namespace,
        "--ignore-not-found",
    )

    endpoint_config = [
        {
            "endpoint": ctx.prom_url,
            "skipTLSVerify": True,
            "tokenFile": SA_TOKEN_PATH,
            "step": f"{args.step_seconds}s",
            "metrics": [f"{CONFIGMAP_MOUNT}/{p.name}" for p in ctx.metric_profiles],
            "indexer": {
                "type": "local",
                "metricsDirectory": RESULTS_DIR,
            },
        }
    ]

    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    config_path = src_dir / "kube-burner-config.yaml"
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(endpoint_config, f, default_flow_style=False, sort_keys=False)

    variables_path = src_dir / "variables.yaml"
    with variables_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(ctx.variables, f, default_flow_style=False, sort_keys=False)

    metric_profiles_dir = src_dir / "metric_profiles"
    metric_profiles_dir.mkdir(parents=True, exist_ok=True)

    KUBE_BURNER_FIELDS = {"query", "metricName", "instant", "captureStart"}
    kb_profiles_dir = src_dir / "kube-burner_profiles"
    kb_profiles_dir.mkdir(parents=True, exist_ok=True)

    from_file_args = [f"--from-file=config.yaml={config_path}"]
    for p in ctx.metric_profiles:
        shutil.copy2(p, metric_profiles_dir / p.name)

        with p.open("r", encoding="utf-8") as f:
            entries = yaml.safe_load(f)
        cleaned = [{k: v for k, v in entry.items() if k in KUBE_BURNER_FIELDS} for entry in entries]
        cleaned_path = kb_profiles_dir / p.name
        with cleaned_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cleaned, f, default_flow_style=False, sort_keys=False)
        from_file_args.append(f"--from-file={p.name}={cleaned_path}")

    oc(
        "create",
        "configmap",
        ctx.configmap_name,
        "-n",
        ctx.prom_namespace,
        *from_file_args,
    )

    return f"ConfigMap {ctx.configmap_name} created with {len(ctx.metric_profiles)} profiles"


@task
def create_capture_pod(args, ctx):
    """Create the kube-burner capture pod with a sidecar for result extraction."""
    best_effort_oc(
        "delete",
        "pod",
        ctx.pod_name,
        "-n",
        ctx.prom_namespace,
        "--ignore-not-found",
    )

    ctx.manifest_file = args.artifact_dir / "src" / "capture-pod.yaml"
    shell.mkdir(ctx.manifest_file.parent)

    template.render_template_to_file(
        "capture-pod.yaml.j2",
        ctx.manifest_file,
        extra_context={
            "configmap_mount": CONFIGMAP_MOUNT,
            "results_dir": RESULTS_DIR,
            "sidecar_image": SIDECAR_IMAGE,
        },
    )

    oc("apply", "-f", str(ctx.manifest_file))

    return f"Pod {ctx.pod_name} created"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_for_pod_start(args, ctx):
    """Wait for the capture pod to start running."""
    result = oc(
        "get",
        "pod",
        ctx.pod_name,
        "-n",
        ctx.prom_namespace,
        "-o",
        "jsonpath={.status.phase}",
        log_stdout=True,
    )

    phase = result.stdout.strip()
    if phase in ("Failed", "Unknown"):
        raise RuntimeError(f"Pod {ctx.pod_name} entered phase {phase}")

    return phase == "Running" or phase == "Succeeded"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_for_capture(args, ctx):
    """Wait for the kube-burner container to finish its capture."""
    result = oc(
        "get",
        "pod",
        ctx.pod_name,
        "-n",
        ctx.prom_namespace,
        "-o",
        'jsonpath={.status.containerStatuses[?(@.name=="kube-burner")].state.terminated.exitCode}',
        log_stdout=True,
    )

    exit_code = result.stdout.strip()
    if not exit_code:
        return False

    if exit_code != "0":
        oc(
            "logs",
            ctx.pod_name,
            "-n",
            ctx.prom_namespace,
            "-c",
            "kube-burner",
            check=False,
        )
        raise RuntimeError(f"kube-burner exited with code {exit_code}")

    return True


@task
def collect_results(args, ctx):
    """Copy metric result JSON files from the sidecar container."""
    oc_cp_from_pod(
        f"{RESULTS_DIR}/.",
        str(ctx.output_dir),
        namespace=ctx.prom_namespace,
        pod=ctx.pod_name,
        container="sidecar",
    )

    result_files = list(ctx.output_dir.glob("*.json"))
    return f"Collected {len(result_files)} metric result files"


@always
@task
def save_kube_burner_logs(args, ctx):
    """Save the kube-burner container logs to the artifact directory."""
    pod_name = getattr(ctx, "pod_name", None)
    prom_namespace = getattr(ctx, "prom_namespace", PROM_NAMESPACE)
    if not pod_name:
        return "No pod name available, skipping log capture"

    logs_dir = args.artifact_dir / "artifacts" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "logs",
        pod_name,
        "-n",
        prom_namespace,
        "-c",
        "kube-burner",
        stdout_dest=logs_dir / "kube-burner.log",
        check=False,
    )
    return f"Logs saved to {logs_dir}"


@always
@task
def save_pod_yaml(args, ctx):
    """Save the capture pod yaml to the artifact directory."""
    pod_name = getattr(ctx, "pod_name", None)
    prom_namespace = getattr(ctx, "prom_namespace", PROM_NAMESPACE)
    if not pod_name:
        return "No pod name available, skipping pod yaml"

    dest_dir = args.artifact_dir / "artifacts"
    dest_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "get",
        "pod",
        pod_name,
        "-n",
        prom_namespace,
        "-oyaml",
        stdout_dest=dest_dir / "capture-pod.yaml",
        check=False,
    )
    return f"Pod yaml saved to {dest_dir}"


@always
@task
def save_pod_description(args, ctx):
    """Save the capture pod description to the artifact directory."""
    pod_name = getattr(ctx, "pod_name", None)
    prom_namespace = getattr(ctx, "prom_namespace", PROM_NAMESPACE)
    if not pod_name:
        return "No pod name available, skipping pod description"

    dest_dir = args.artifact_dir / "artifacts"
    dest_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "describe",
        "pod",
        pod_name,
        "-n",
        prom_namespace,
        stdout_dest=dest_dir / "capture-pod.desc.txt",
        check=False,
    )
    return f"Pod description saved to {dest_dir}"


@always
@task
def cleanup(args, ctx):
    """Delete the capture pod and ConfigMap."""
    pod_name = getattr(ctx, "pod_name", None)
    configmap_name = getattr(ctx, "configmap_name", None)
    prom_namespace = getattr(ctx, "prom_namespace", PROM_NAMESPACE)

    if pod_name:
        best_effort_oc("delete", "pod", pod_name, "-n", prom_namespace, "--ignore-not-found")
    if configmap_name:
        best_effort_oc(
            "delete",
            "configmap",
            configmap_name,
            "-n",
            prom_namespace,
            "--ignore-not-found",
        )

    return "Cleaned up capture pod and configmap"


if __name__ == "__main__":
    run.main()
