#!/usr/bin/env python3

from __future__ import annotations

import gzip
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from projects.core.dsl import (  # noqa: F401
    always,
    entrypoint,
    execute_tasks,
    on_failure,
    retry,
    task,
)
from projects.core.dsl.utils import write_json, write_text
from projects.core.dsl.utils.k8s import (
    oc,
    oc_apply,
    oc_get_json,
)
from projects.guidellm.toolbox.run_guidellm_benchmark.utils import (
    expand_guidellm_runs,
    render_guidellm_copy_pod_from_parts,
    render_guidellm_job_from_parts,
    render_guidellm_pvc_from_parts,
    render_guidellm_shared_volume_job_from_parts,
)

logger = logging.getLogger(__name__)
WAIT_POLL_INTERVAL_SECONDS = 10
JOB_COMPLETION_GRACE_SECONDS = 60
DEFAULT_GUIDELLM_IMAGE = "ghcr.io/vllm-project/guidellm:v0.8.1"

# Script template for trimming benchmark JSON on remote pod (removes 'requests' field)
# Use REMOTE_TRIM_SCRIPT.format(file_path=...) to generate the script
# Removes bulky text fields (request_args, output, reasoning_output) from request
# entries while preserving per-request metrics like token counts, latencies, etc.
REMOTE_TRIM_SCRIPT = """
import json
import os

BULK_FIELDS = {{"request_args", "output", "reasoning_output"}}

def trim_entry(obj):
    if isinstance(obj, dict):
        return {{k: trim_entry(v) for k, v in obj.items() if k not in BULK_FIELDS}}
    if isinstance(obj, list):
        return [trim_entry(i) for i in obj]
    return obj

def trim(obj):
    if isinstance(obj, dict):
        return {{k: trim_entry(v) if k == "requests" else trim(v) for k, v in obj.items()}}
    if isinstance(obj, list):
        return [trim(i) for i in obj]
    return obj

tmp_path = '{file_path}.trimtmp'
with open('{file_path}') as f:
    data = json.load(f)
try:
    with open(tmp_path, 'w') as f:
        json.dump(trim(data), f, separators=(',', ':'))
    os.replace(tmp_path, '{file_path}')
except Exception:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    raise
""".strip()


_REQUEST_BULK_FIELDS = {"request_args", "output", "reasoning_output"}


def trim_benchmark_json(obj):
    """Trim bulky text from request entries while keeping per-turn metrics.

    Strips ``request_args`` (full message history), ``output``, and
    ``reasoning_output`` from each request entry. Small fields like
    ``info`` (conversation_id, turn_index, timings), token counts, and
    computed metrics (TTFT, ITL, latency, tokens/sec) are preserved so
    that per-turn analysis is possible from the trimmed file.
    """
    if isinstance(obj, dict):
        return {
            k: _trim_request_entry(v) if k == "requests" else trim_benchmark_json(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [trim_benchmark_json(item) for item in obj]
    else:
        return obj


def _trim_request_entry(obj):
    """Recursively trim bulky text fields from request entries."""
    if isinstance(obj, dict):
        return {k: _trim_request_entry(v) for k, v in obj.items() if k not in _REQUEST_BULK_FIELDS}
    elif isinstance(obj, list):
        return [_trim_request_entry(item) for item in obj]
    else:
        return obj


@entrypoint
def run(
    *,
    endpoint_url: str,
    name: str = "guidellm-benchmark",
    namespace: str = "",
    image: str | None = DEFAULT_GUIDELLM_IMAGE,
    timeout: int = 900,
    pvc_size: str = "1Gi",
    pvc_storage_class: str | None = None,
    guidellm_args: list[str] | None = None,
    config_path: Path | None = None,
    hf_token_secret: str = "",
    fs_group: int | None = None,
    keep_full_benchmark_file: bool = False,
    use_pvc: bool = False,
    api_key_file_path: str | None = None,
    replica_count: int = 1,
    validate_backend: bool = True,
) -> int:
    """
    Run the GuideLLM benchmark against a resolved endpoint.

    Args:
        endpoint_url: Endpoint URL for the LLM inference service to benchmark
        name: Name of the benchmark job
        namespace: Namespace to run the benchmark job in (empty string auto-detects current namespace)
        image: Full container image reference for the benchmark (None uses the toolbox default)
        timeout: Active deadline for the Job and timeout in seconds to wait for completion
        pvc_size: Size of the PersistentVolumeClaim for storing results (only used in PVC mode)
        guidellm_args: List of additional guidellm arguments (e.g., ["--profile=kind=concurrent,streams=10"])
        config_path: Path to a GuideLLM config YAML file on the local filesystem.
            When set, the file is read and embedded in the container, and GuideLLM
            is invoked with ``--config`` pointing to it.
        hf_token_secret: Name of the K8s secret containing HF_TOKEN. If empty, HF_TOKEN is not injected.
        fs_group: If set, adds securityContext.fsGroup to the GuideLLM job pod.
        keep_full_benchmark_file: Whether to keep the full untrimmed benchmark JSON files alongside trimmed ones (default: False)
        use_pvc: Use PVC with copy pod instead of shared emptyDir volume with sidecar (default: False)
        api_key_file_path: Path to a local file containing the API key for the
            backend. When set, a K8s Secret is created, mounted into the pod, and
            injected into the ``--backend`` argument. The secret is owned by the
            Job for automatic cleanup.
        replica_count: Number of parallel Job replicas. Each replica writes to its
            own output file using ``JOB_COMPLETION_INDEX``.
        validate_backend: Whether GuideLLM should validate the backend before
            benchmarking. Set to False for endpoints that don't support /v1/models.
    """

    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    # None means "no image configured": use the toolbox default
    if not image:
        image = DEFAULT_GUIDELLM_IMAGE

    execute_tasks(locals())
    return 0


@task
def validate_parameters(args, ctx):
    """Validate and normalize parameters"""

    # Ensure guidellm_args is a list
    ctx.guidellm_args = args.guidellm_args or []
    ctx.guidellm_runs = expand_guidellm_runs(ctx.guidellm_args)

    # Auto-detect namespace if empty
    if not args.namespace:
        result = oc("project", "-q", check=False)
        if result.returncode == 0:
            ctx.target_namespace = result.stdout.strip()
        else:
            raise RuntimeError("Could not auto-detect current namespace")
    else:
        ctx.target_namespace = args.namespace

    benchmark_name = args.name
    if args.artifact_dirname_suffix:
        benchmark_name = f"{benchmark_name}-{args.artifact_dirname_suffix.replace('_', '-')}"
    ctx.benchmark_name = benchmark_name
    ctx.image = args.image

    return f"Validated parameters for benchmark {ctx.benchmark_name} in namespace {ctx.target_namespace}"


@task
def cleanup_previous_guidellm_resources_task(args, ctx):
    """Delete previous GuideLLM benchmark helper resources"""

    if args.use_pvc:
        _best_effort_delete(
            "GuideLLM benchmark copy pod",
            "delete",
            "pod",
            f"{ctx.benchmark_name}-copy",
            "-n",
            ctx.target_namespace,
            "--ignore-not-found=true",
        )
        _best_effort_delete(
            "GuideLLM benchmark PVC",
            "delete",
            "pvc",
            ctx.benchmark_name,
            "-n",
            ctx.target_namespace,
            "--ignore-not-found=true",
        )

    _best_effort_delete(
        "GuideLLM benchmark job",
        "delete",
        "job",
        ctx.benchmark_name,
        "-n",
        ctx.target_namespace,
        "--ignore-not-found=true",
    )

    mode = "PVC" if args.use_pvc else "shared volume"
    return f"Deleted previous GuideLLM resources for {ctx.benchmark_name} ({mode} mode)"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_old_pods_gone(args, ctx):
    """Wait for old GuideLLM benchmark pods to disappear."""
    result = oc(
        "get",
        "pods",
        "-n",
        ctx.target_namespace,
        "-l",
        f"job-name={ctx.benchmark_name}",
        "--ignore-not-found=true",
        "--no-headers",
        check=False,
    )

    if result.stdout.strip():
        return False

    return f"Old pods gone for {ctx.benchmark_name}"


def _best_effort_delete(description: str, *oc_args: str) -> None:
    try:
        oc(*oc_args, check=False, timeout_seconds=60)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out deleting %s: oc %s", description, " ".join(oc_args))


@task
def create_api_key_secret(args, ctx):
    """Create a K8s Secret from the API key file (if provided)"""

    if not args.api_key_file_path:
        ctx.api_key_secret_name = ""
        return "No API key file provided"

    token_file = Path(args.api_key_file_path)
    if not token_file.exists():
        raise FileNotFoundError(f"API key file does not exist: {args.api_key_file_path}")

    secret_name = f"{ctx.benchmark_name}-auth-token"

    oc(
        "delete",
        "secret",
        secret_name,
        "-n",
        ctx.target_namespace,
        "--ignore-not-found=true",
        check=False,
    )

    # secret is safe here (using --from-file)
    oc(
        "create",
        "secret",
        "generic",
        secret_name,
        f"--from-file=auth_token={args.api_key_file_path}",
        "-n",
        ctx.target_namespace,
    )

    ctx.api_key_secret_name = secret_name
    return f"API key secret {secret_name} created from file"


@task
def create_guidellm_resources_task(args, ctx):
    """Create the GuideLLM benchmark job and optionally PVC with job as owner"""

    # Ensure src directory exists
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Copy and validate the config file into the artifact src/ directory
    ctx.config_path = args.config_path
    if ctx.config_path is not None:
        dest = src_dir / "guidellm-config.yaml"
        shutil.copy2(ctx.config_path, dest)
        # Validate the YAML is well-formed
        yaml.safe_load(dest.read_text())
        ctx.config_path = dest

    api_key_secret = getattr(ctx, "api_key_secret_name", "") or ""

    # Create the job based on mode
    if args.use_pvc:
        if args.replica_count > 1:
            raise ValueError(
                f"PVC mode does not support replica_count > 1 (got {args.replica_count}). "
                "Use shared-volume mode (use_pvc=False) for multi-replica benchmarks."
            )

        # PVC mode - traditional single container job + PVC
        oc_apply(
            src_dir / "guidellm-job.yaml",
            render_guidellm_job_from_parts(
                namespace=ctx.target_namespace,
                name=ctx.benchmark_name,
                image=ctx.image,
                endpoint_url=args.endpoint_url,
                guidellm_args=ctx.guidellm_args,
                timeout_seconds=args.timeout,
                hf_token_secret=args.hf_token_secret,
                fs_group=args.fs_group,
                config_path=ctx.config_path,
                api_key_secret=api_key_secret,
                replica_count=args.replica_count,
                validate_backend=args.validate_backend,
            ),
        )

        # Get the job metadata for owner reference
        job_data = oc_get_json("job", name=ctx.benchmark_name, namespace=ctx.target_namespace)

        # Create owner reference from job metadata
        owner_reference = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_data["metadata"]["name"],
            "uid": job_data["metadata"]["uid"],
            "controller": True,
            "blockOwnerDeletion": True,
        }

        # Create the PVC with job as owner
        oc_apply(
            src_dir / "guidellm-pvc.yaml",
            render_guidellm_pvc_from_parts(
                namespace=ctx.target_namespace,
                name=ctx.benchmark_name,
                pvc_size=args.pvc_size,
                pvc_storage_class=args.pvc_storage_class,
                owner_reference=owner_reference,
            ),
        )

        # Set ownerReference on the API key secret
        if api_key_secret:
            _set_secret_owner_reference(ctx, owner_reference)

        ctx.wait_deadline = time.monotonic() + args.timeout + JOB_COMPLETION_GRACE_SECONDS
        return f"GuideLLM benchmark {ctx.benchmark_name} created with job as PVC owner"
    else:
        # Shared volume mode - job with main + sidecar containers
        oc_apply(
            src_dir / "guidellm-job.yaml",
            render_guidellm_shared_volume_job_from_parts(
                namespace=ctx.target_namespace,
                name=ctx.benchmark_name,
                image=ctx.image,
                endpoint_url=args.endpoint_url,
                guidellm_args=ctx.guidellm_args,
                timeout_seconds=args.timeout,
                hf_token_secret=args.hf_token_secret,
                fs_group=args.fs_group,
                config_path=ctx.config_path,
                api_key_secret=api_key_secret,
                replica_count=args.replica_count,
                validate_backend=args.validate_backend,
            ),
        )

        # Set ownerReference on the API key secret
        if api_key_secret:
            job_data = oc_get_json("job", name=ctx.benchmark_name, namespace=ctx.target_namespace)
            owner_reference = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": job_data["metadata"]["name"],
                "uid": job_data["metadata"]["uid"],
                "controller": True,
                "blockOwnerDeletion": True,
            }
            _set_secret_owner_reference(ctx, owner_reference)

        ctx.wait_deadline = time.monotonic() + args.timeout + JOB_COMPLETION_GRACE_SECONDS
        return (
            f"GuideLLM benchmark {ctx.benchmark_name} created with shared volume (main + sidecar)"
        )


def _set_secret_owner_reference(ctx, owner_reference):
    """Patch the API key secret with an ownerReference pointing to the Job."""
    oc(
        "patch",
        "secret",
        ctx.api_key_secret_name,
        "-n",
        ctx.target_namespace,
        "--type=merge",
        "-p",
        json.dumps({"metadata": {"ownerReferences": [owner_reference]}}),
    )
    logger.info(
        "Set ownerReference on secret %s -> job %s",
        ctx.api_key_secret_name,
        owner_reference["name"],
    )


def _handle_benchmark_wait_failure(args, ctx, exception):
    """Generate AGENT.md file for post-mortem analysis of benchmark failure."""
    benchmark_name = getattr(ctx, "benchmark_name", args.name)
    namespace = getattr(ctx, "target_namespace", args.namespace)

    agent_md_path = args.artifact_dir / "AGENT.md"
    with open(agent_md_path, "w", encoding="utf-8") as f:
        f.write(f"""# Post-Mortem: GuideLLM Benchmark Failure

## Failure Details
- **Benchmark**: {benchmark_name}
- **Namespace**: {namespace}
- **Failed Task**: wait_guidellm_benchmark_task
- **Exception**: {exception.__class__.__name__}: {exception}

## Available Files for Analysis
- **Pod logs**: `artifacts/logs/guidellm_benchmark_<pod_name>.log` (one per pod)
- **Pod YAML**: `artifacts/guidellm_benchmark_job.pods.yaml`
- **Job YAML**: `artifacts/guidellm_benchmark_job.yaml`
- **Pod description**: `artifacts/guidellm_benchmark_pod_description.txt`

## Analysis Instructions
1. **Review pod logs** in `artifacts/logs/guidellm_benchmark_<pod_name>.log` for GuideLLM errors or crashes
2. **Check pod status** in `guidellm_benchmark_job.pods.yaml` for OOM kills, image pull errors, or scheduling failures
3. **Examine job YAML** for timeout or backoff limit issues
4. **Check pod description** for K8s events (resource limits, node affinity, etc.)
""")


# An upper safety bound only; ctx.wait_deadline enforces the per-run timeout.
@on_failure(_handle_benchmark_wait_failure)
@retry(attempts=1080, delay=WAIT_POLL_INTERVAL_SECONDS, backoff=1.0)
@task
def wait_guidellm_benchmark_task(args, ctx):
    """Wait for the GuideLLM benchmark to finish running"""

    if args.use_pvc:
        return _poll_job_active(args, ctx)
    else:
        return _poll_main_container_running(args, ctx)


@task
def check_guidellm_benchmark_result(args, ctx):
    """Check whether the completed benchmark succeeded or failed"""

    if args.use_pvc:
        return _check_job_outcome(args, ctx)
    else:
        return _check_main_container_outcome(args, ctx)


def _poll_job_active(args, ctx):
    """Poll until the job is no longer active (PVC mode)."""
    if time.monotonic() >= ctx.wait_deadline:
        raise TimeoutError(
            f"GuideLLM benchmark {ctx.benchmark_name} did not complete within {args.timeout}s"
        )

    active_result = oc(
        "get",
        "job",
        ctx.benchmark_name,
        "-n",
        ctx.target_namespace,
        "-o",
        "jsonpath={.status.active}",
        check=False,
    )

    active_str = active_result.stdout.strip() if active_result.returncode == 0 else ""
    if active_str in ("", "0"):
        return f"Job {ctx.benchmark_name} is no longer active"

    oc(
        "get",
        "pods",
        "-n",
        ctx.target_namespace,
        "-l",
        f"job-name={ctx.benchmark_name}",
        "-o",
        "wide",
    )

    logger.info("Job %s is still active, retrying...", ctx.benchmark_name)
    return False


def _check_job_outcome(args, ctx):
    """Inspect the final status of a completed job (PVC mode)."""
    succeeded_result = oc(
        "get",
        "job",
        ctx.benchmark_name,
        "-n",
        ctx.target_namespace,
        "-o",
        "jsonpath={.status.succeeded}",
        check=False,
        log_stdout=True,
    )
    failed_result = oc(
        "get",
        "job",
        ctx.benchmark_name,
        "-n",
        ctx.target_namespace,
        "-o",
        "jsonpath={.status.failed}",
        check=False,
        log_stdout=True,
    )

    succeeded_str = succeeded_result.stdout.strip() if succeeded_result.returncode == 0 else ""
    succeeded_count = int(succeeded_str) if succeeded_str else 0
    failed_str = failed_result.stdout.strip() if failed_result.returncode == 0 else ""
    failed_count = int(failed_str) if failed_str else 0

    logger.info(
        "Job %s final status - succeeded: %d, failed: %d, expected: %d",
        ctx.benchmark_name,
        succeeded_count,
        failed_count,
        args.replica_count,
    )

    failure_file = args.artifact_dir / "FAILURE.txt"
    if failed_count:
        failure_message = f"""GuideLLM benchmark job '{ctx.benchmark_name}' failed.

Check the per-pod log files for detailed error information:
  artifacts/logs/guidellm_benchmark_<pod_name>.log
"""
        write_text(failure_file, failure_message)
        raise RuntimeError(f"GuideLLM job {ctx.benchmark_name} failed")

    if succeeded_count == args.replica_count:
        return f"GuideLLM benchmark {ctx.benchmark_name} succeeded ({succeeded_count}/{args.replica_count} replicas)"

    failure_message = f"""GuideLLM benchmark job '{ctx.benchmark_name}' ended with unexpected replica counts (succeeded={succeeded_count}, expected={args.replica_count}).

Check the per-pod log files for detailed error information:
  artifacts/logs/guidellm_benchmark_<pod_name>.log
"""
    write_text(failure_file, failure_message)
    raise RuntimeError(
        f"GuideLLM job {ctx.benchmark_name} ended with unexpected replica counts"
        f" (succeeded={succeeded_count}, expected={args.replica_count})"
    )


def _get_job_pod_names(namespace: str, benchmark_name: str) -> list[str]:
    """Get all pod names for a job, sorted for determinism."""
    pod_result = oc(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        f"job-name={benchmark_name}",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        check=False,
    )
    if pod_result.returncode != 0 or not pod_result.stdout.strip():
        return []
    return sorted(line for line in pod_result.stdout.strip().splitlines() if line)


def _poll_main_container_running(args, ctx):
    """Poll until all main containers terminate (shared volume mode)."""
    if time.monotonic() >= ctx.wait_deadline:
        raise TimeoutError(
            f"GuideLLM benchmark {ctx.benchmark_name} did not complete within {args.timeout}s"
        )

    pod_names = _get_job_pod_names(ctx.target_namespace, ctx.benchmark_name)
    if not pod_names:
        logger.info("No pods for job %s yet, retrying...", ctx.benchmark_name)
        return False

    expected = args.replica_count
    if len(pod_names) < expected:
        logger.info(
            "Only %d/%d pods for job %s, retrying...",
            len(pod_names),
            expected,
            ctx.benchmark_name,
        )
        return False

    exit_codes: dict[str, int] = {}
    for pod_name in pod_names:
        main_status_result = oc(
            "get",
            "pod",
            pod_name,
            "-n",
            ctx.target_namespace,
            "-o",
            "jsonpath={.status.containerStatuses[?(@.name=='guidellm-main')].state}",
            check=False,
        )

        if main_status_result.returncode != 0:
            logger.info("Could not get main container status for %s, retrying...", pod_name)
            return False

        try:
            container_state = json.loads(main_status_result.stdout.strip())
        except (json.JSONDecodeError, AttributeError):
            logger.info("Main container state not available for %s, retrying...", pod_name)
            return False

        if "terminated" not in container_state:
            logger.info("Main container for pod %s still running, retrying...", pod_name)
            return False

        exit_codes[pod_name] = container_state["terminated"].get("exitCode", 0)

    ctx.main_container_exit_codes = exit_codes
    summary = ", ".join(f"{p}={c}" for p, c in exit_codes.items())
    return f"All main containers for {ctx.benchmark_name} terminated (exit codes: {summary})"


def _check_main_container_outcome(args, ctx):
    """Inspect the exit codes of terminated main containers (shared volume mode)."""
    exit_codes = ctx.main_container_exit_codes
    failed_pods = {pod: code for pod, code in exit_codes.items() if code != 0}

    if not failed_pods:
        return f"GuideLLM benchmark main containers {ctx.benchmark_name} all succeeded"

    failure_file = args.artifact_dir / "FAILURE.txt"
    details = ", ".join(f"{pod} (exit {code})" for pod, code in failed_pods.items())
    failure_message = (
        f"GuideLLM benchmark main container(s) '{ctx.benchmark_name}' failed: {details}.\n\n"
        "Check the job logs for detailed error information:\n"
        "  artifacts/logs/guidellm_benchmark_<pod_name>.log\n"
    )
    write_text(failure_file, failure_message)
    raise RuntimeError(f"GuideLLM main container(s) {ctx.benchmark_name} failed: {details}")


@always
@task
def capture_guidellm_state_task(args, ctx):
    """Capture GuideLLM benchmark job state and logs"""

    capture_guidellm_state(
        artifact_dir=args.artifact_dir,
        namespace=ctx.target_namespace,
        benchmark_name=ctx.benchmark_name,
    )
    return f"GuideLLM benchmark {ctx.benchmark_name} state captured"


@task
def create_copy_pod(args, ctx):
    """Create copy pod for GuideLLM results (PVC mode only)"""

    if not args.use_pvc:
        return "Skipping copy pod creation for shared volume mode"

    pod_data = oc_get_json(
        "pods",
        namespace=ctx.target_namespace,
        selector=f"job-name={ctx.benchmark_name}",
        ignore_not_found=True,
    )
    node_name = None
    if pod_data and pod_data.get("items"):
        node_name = pod_data["items"][0].get("spec", {}).get("nodeName")

    # Helper commands need shell utilities that custom benchmark images may lack.
    oc_apply(
        args.artifact_dir / "src" / "guidellm-copy-pod.yaml",
        render_guidellm_copy_pod_from_parts(
            namespace=ctx.target_namespace,
            name=ctx.benchmark_name,
            image=args.image,
            pvc_size=args.pvc_size,
            node_name=node_name,
        ),
    )
    return f"Created copy pod {ctx.benchmark_name}-copy"


@retry(attempts=60, delay=5, backoff=1.0)
@task
def wait_copy_pod_ready(args, ctx):
    """Wait for copy pod to be ready (PVC mode only)"""

    if not args.use_pvc:
        return "Skipping copy pod wait for shared volume mode"

    payload = oc_get_json(
        "pod",
        name=f"{ctx.benchmark_name}-copy",
        namespace=ctx.target_namespace,
    )
    conditions = payload.get("status", {}).get("conditions", [])
    if any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    ):
        return f"Copy pod {ctx.benchmark_name}-copy ready"
    return False  # Retry


@task
def extract_results(args, ctx):
    """Extract GuideLLM results from copy pod (PVC mode) or sidecar (shared volume mode)"""

    results_dir = args.artifact_dir / "artifacts" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.use_pvc:
        return extract_results_from_copy_pod(args, ctx, results_dir)
    else:
        return extract_results_from_sidecar(args, ctx, results_dir)


def _extract_from_copy_pod_file(
    args,
    ctx,
    results_dir: Path,
    run,
    replica_suffix: str,
) -> dict[str, str | None]:
    """Extract a single benchmark result file from the copy pod."""
    copy_pod = f"{ctx.benchmark_name}-copy"
    remote_path = f"/results/benchmarks-{run.label}{replica_suffix}.json"
    local_path = results_dir / f"benchmarks-{run.label}{replica_suffix}.json"

    logger.info(
        "Retrieving the compressed benchmark file for %s%s...",
        run.label,
        replica_suffix,
    )

    temp_gz_path = local_path.with_suffix(".json.gz")
    remote_gz_path = f"{remote_path}.gz"

    if not args.keep_full_benchmark_file:
        logger.info("Trimming benchmark file on copy pod for %s%s...", run.label, replica_suffix)
        trim_result = oc(
            "exec",
            "-n",
            ctx.target_namespace,
            copy_pod,
            "--",
            "python3",
            "-c",
            REMOTE_TRIM_SCRIPT.format(file_path=remote_path),
            check=False,
            timeout_seconds=1800,
        )
        if trim_result.returncode != 0:
            logger.warning(
                "Failed to trim on copy pod for %s%s, will trim locally: %s",
                run.label,
                replica_suffix,
                trim_result.stderr,
            )

    compress_result = oc(
        "exec",
        "-n",
        ctx.target_namespace,
        copy_pod,
        "--",
        "gzip",
        "-kf",
        remote_path,
        check=False,
        timeout_seconds=1800,
    )
    if compress_result.returncode != 0:
        raise RuntimeError(
            f"Failed to create compressed results for {ctx.benchmark_name} run {run.label}{replica_suffix}"
        )

    result = oc(
        "cp",
        "-n",
        ctx.target_namespace,
        f"{copy_pod}:{remote_gz_path}",
        str(temp_gz_path),
        check=False,
        timeout_seconds=1800,
    )
    if result.returncode != 0 or not temp_gz_path.exists() or temp_gz_path.stat().st_size == 0:
        raise RuntimeError(
            f"Failed to copy compressed results for {ctx.benchmark_name} run {run.label}{replica_suffix}"
        )

    logger.info("Extracting compressed benchmark file for %s%s...", run.label, replica_suffix)
    try:
        with gzip.open(temp_gz_path, "rt", encoding="utf-8") as f:
            json_content = f.read()
    except Exception as e:
        raise RuntimeError(
            f"Failed to extract compressed results for run {run.label}{replica_suffix}: {e}"
        ) from e

    logger.info("Trimming the benchmark.json file for %s%s...", run.label, replica_suffix)
    try:
        raw_data = json.loads(json_content)
        cleaned_data = trim_benchmark_json(raw_data)
        with open(local_path, "w") as f:
            json.dump(cleaned_data, f, separators=(",", ":"))
    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON for %s%s, writing raw data", run.label, replica_suffix)
        write_text(local_path, json_content)

    if not args.keep_full_benchmark_file:
        if temp_gz_path.exists():
            temp_gz_path.unlink()
            logger.info("Removed full benchmark file %s", temp_gz_path.name)
    else:
        logger.info("Keeping full benchmark file %s", temp_gz_path.name)

    entry: dict[str, str | None] = {
        "label": run.label,
        "rate": run.rate,
        "remote_path": remote_path,
        "local_path": str(local_path.relative_to(args.artifact_dir)),
    }
    if replica_suffix:
        entry["replica"] = replica_suffix.lstrip("-")
    return entry


def extract_results_from_copy_pod(args, ctx, results_dir: Path):
    """Extract results from copy pod (PVC mode)"""
    extracted_files: list[dict[str, str | None]] = []

    if args.replica_count > 1:
        replica_suffixes = [f"-{i}" for i in range(args.replica_count)]
    else:
        replica_suffixes = [""]

    for run in ctx.guidellm_runs:
        for replica_suffix in replica_suffixes:
            extracted_files.append(
                _extract_from_copy_pod_file(args, ctx, results_dir, run, replica_suffix)
            )

    write_json(
        results_dir / "index.json",
        {
            "benchmark_name": ctx.benchmark_name,
            "runs": extracted_files,
        },
    )

    return f"Extracted results for {ctx.benchmark_name} from copy pod ({len(replica_suffixes)} replica(s))"


def _get_pod_completion_index(namespace: str, pod_name: str) -> str:
    """Get the JOB_COMPLETION_INDEX label from a pod."""
    result = oc(
        "get",
        "pod",
        pod_name,
        "-n",
        namespace,
        "-o",
        "jsonpath={.metadata.labels.batch\\.kubernetes\\.io/job-completion-index}",
        check=False,
        log_stdout=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _extract_from_sidecar_pod(
    args,
    ctx,
    pod_name: str,
    results_dir: Path,
    replica_suffix: str,
) -> list[dict[str, str | None]]:
    """Extract benchmark results from a single pod's sidecar container."""
    extracted_files: list[dict[str, str | None]] = []

    for run in ctx.guidellm_runs:
        remote_path = f"/results/benchmarks-{run.label}{replica_suffix}.json"
        local_path = results_dir / f"benchmarks-{run.label}{replica_suffix}.json"

        logger.info(
            "Retrieving the compressed benchmark file for %s%s from sidecar %s...",
            run.label,
            replica_suffix,
            pod_name,
        )

        temp_gz_path = local_path.with_suffix(".json.gz")
        remote_gz_path = f"{remote_path}.gz"

        if not args.keep_full_benchmark_file:
            logger.info("Trimming benchmark file on sidecar for %s%s...", run.label, replica_suffix)
            trim_result = oc(
                "exec",
                "-n",
                ctx.target_namespace,
                pod_name,
                "-c",
                "guidellm-sidecar",
                "--",
                "python3",
                "-c",
                REMOTE_TRIM_SCRIPT.format(file_path=remote_path),
                check=False,
                timeout_seconds=1800,
            )
            if trim_result.returncode != 0:
                logger.warning(
                    "Failed to trim on sidecar for %s%s, will trim locally: %s",
                    run.label,
                    replica_suffix,
                    trim_result.stderr,
                )

        compress_result = oc(
            "exec",
            "-n",
            ctx.target_namespace,
            pod_name,
            "-c",
            "guidellm-sidecar",
            "--",
            "gzip",
            "-kf",
            remote_path,
            check=False,
            timeout_seconds=1800,
        )
        if compress_result.returncode != 0:
            raise RuntimeError(
                f"Failed to create compressed results for {ctx.benchmark_name} run {run.label}{replica_suffix}"
            )

        result = oc(
            "cp",
            "-n",
            ctx.target_namespace,
            "-c",
            "guidellm-sidecar",
            f"{pod_name}:{remote_gz_path}",
            str(temp_gz_path),
            check=False,
            timeout_seconds=1800,
        )
        if result.returncode != 0 or not temp_gz_path.exists() or temp_gz_path.stat().st_size == 0:
            raise RuntimeError(
                f"Failed to copy compressed results for {ctx.benchmark_name} run {run.label}{replica_suffix}"
            )

        logger.info("Extracting compressed benchmark file for %s%s...", run.label, replica_suffix)
        try:
            with gzip.open(temp_gz_path, "rt", encoding="utf-8") as f:
                json_content = f.read()
        except Exception as e:
            raise RuntimeError(
                f"Failed to extract compressed results for run {run.label}{replica_suffix}: {e}"
            ) from e

        logger.info("Trimming the benchmark.json file for %s%s...", run.label, replica_suffix)
        try:
            raw_data = json.loads(json_content)
            cleaned_data = trim_benchmark_json(raw_data)
            with open(local_path, "w") as f:
                json.dump(cleaned_data, f, separators=(",", ":"))
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse JSON for %s%s, writing raw data", run.label, replica_suffix
            )
            write_text(local_path, json_content)

        if not args.keep_full_benchmark_file:
            if temp_gz_path.exists():
                temp_gz_path.unlink()
                logger.info("Removed full benchmark file %s", temp_gz_path.name)
        else:
            logger.info("Keeping full benchmark file %s", temp_gz_path.name)

        entry: dict[str, str | None] = {
            "label": run.label,
            "rate": run.rate,
            "remote_path": remote_path,
            "local_path": str(local_path.relative_to(args.artifact_dir)),
        }
        if replica_suffix:
            entry["replica"] = replica_suffix.lstrip("-")
        extracted_files.append(entry)

    # Signal sidecar to exit
    logger.info("Creating done file to signal sidecar to exit on %s...", pod_name)
    oc(
        "exec",
        "-n",
        ctx.target_namespace,
        pod_name,
        "-c",
        "guidellm-sidecar",
        "--",
        "touch",
        "/results/done",
        check=True,
    )

    return extracted_files


def extract_results_from_sidecar(args, ctx, results_dir: Path):
    """Extract results from sidecar container(s) (shared volume mode)"""

    pod_names = _get_job_pod_names(ctx.target_namespace, ctx.benchmark_name)
    if not pod_names:
        raise RuntimeError(f"Could not find pods for job {ctx.benchmark_name}")

    extracted_files: list[dict[str, str | None]] = []

    if args.replica_count <= 1:
        extracted_files.extend(_extract_from_sidecar_pod(args, ctx, pod_names[0], results_dir, ""))
    else:
        for pod_name in pod_names:
            index = _get_pod_completion_index(ctx.target_namespace, pod_name)
            if not index:
                raise RuntimeError(f"Could not get completion index for pod {pod_name}")
            replica_suffix = f"-{index}"
            extracted_files.extend(
                _extract_from_sidecar_pod(args, ctx, pod_name, results_dir, replica_suffix)
            )

    write_json(
        results_dir / "index.json",
        {
            "benchmark_name": ctx.benchmark_name,
            "runs": extracted_files,
        },
    )

    return f"Extracted results for {ctx.benchmark_name} from {len(pod_names)} sidecar pod(s)"


def _copy_result_file(
    namespace: str,
    pod: str,
    remote_path: str,
    local_path: Path,
    *,
    trim_json: bool = True,
    keep_full_file: bool = False,
) -> Path | None:
    """Copy a result file from a pod, with optional compression and trimming.

    Args:
        namespace: Kubernetes namespace
        pod: Pod name
        remote_path: Path to file inside the pod
        local_path: Local destination path
        trim_json: Whether to trim large fields from JSON files (default: True)
        keep_full_file: Whether to keep the full untrimmed file alongside trimmed one (default: False)

    Returns:
        Path to the copied file, or None if copy failed
    """
    import gzip

    # Use compression approach for better handling of large files
    logger.info(f"Retrieving compressed file from {remote_path}...")

    # Save compressed version to temp file first
    temp_gz_path = local_path.with_suffix(".json.gz")

    # Compress and stream directly from pod
    result = oc(
        "exec",
        "-n",
        namespace,
        pod,
        "--",
        "gzip",
        "-c",
        remote_path,
        check=False,
        log_stdout=False,
        stdout_dest=temp_gz_path,
        text=False,
    )
    if result.returncode != 0:
        logging.getLogger(__name__).warning("gzip extraction failed (rc=%d)", result.returncode)
        return None

    # Extract and parse JSON from local compressed file
    try:
        with gzip.open(temp_gz_path, "rt", encoding="utf-8") as f:
            json_content = f.read()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to extract compressed results: %s", e)
        temp_gz_path.unlink(missing_ok=True)
        return None

    # Create trimmed version if requested and file is JSON
    if trim_json and local_path.suffix.lower() == ".json":
        logger.info(f"Trimming JSON file {local_path.name}...")
        try:
            raw_data = json.loads(json_content)
            cleaned_data = trim_benchmark_json(raw_data)
            with open(local_path, "w") as f:
                json.dump(cleaned_data, f, separators=(",", ":"))
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON, writing raw data to {local_path}")
            write_text(local_path, json_content)
    else:
        # Write raw content for non-JSON files or when trimming is disabled
        write_text(local_path, json_content)

    # Manage full file based on keep_full_file flag
    if not keep_full_file:
        # Remove the compressed full file
        if temp_gz_path.exists():
            temp_gz_path.unlink()
            logger.info(f"Removed full file {temp_gz_path.name}")
    else:
        logger.info(f"Keeping full file {temp_gz_path.name}")

    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path

    return None


@task
def cleanup_copy_pod(args, ctx):
    """Delete the copy pod after results extraction (PVC mode only)"""

    if not args.use_pvc:
        return "Skipping copy pod cleanup for shared volume mode"

    _best_effort_delete(
        "GuideLLM benchmark copy pod",
        "delete",
        "pod",
        f"{ctx.benchmark_name}-copy",
        "-n",
        ctx.target_namespace,
        "--ignore-not-found=true",
    )
    return f"Cleaned up copy pod {ctx.benchmark_name}-copy"


@always
@task
def cleanup_api_key_secret(args, ctx):
    """Delete the API key secret if it was created (safety net for ownerReference failures)"""

    secret_name = getattr(ctx, "api_key_secret_name", "")
    if not secret_name:
        return "No API key secret to clean up"

    _best_effort_delete(
        "API key secret",
        "delete",
        "secret",
        secret_name,
        "-n",
        ctx.target_namespace,
        "--ignore-not-found=true",
    )
    return f"Cleaned up API key secret {secret_name}"


@always
@task
def cleanup_guidellm_resources(args, ctx):
    """Delete the GuideLLM benchmark job and optionally PVC at the end"""

    _best_effort_delete(
        "GuideLLM benchmark job",
        "delete",
        "job",
        ctx.benchmark_name,
        "-n",
        ctx.target_namespace,
        "--ignore-not-found=true",
    )

    if args.use_pvc:
        _best_effort_delete(
            "GuideLLM benchmark PVC",
            "delete",
            "pvc",
            ctx.benchmark_name,
            "-n",
            ctx.target_namespace,
            "--ignore-not-found=true",
        )

    mode = "PVC" if args.use_pvc else "shared volume"
    return f"Cleaned up GuideLLM benchmark resources for {ctx.benchmark_name} ({mode} mode)"


def capture_guidellm_state(*, artifact_dir: Path, namespace: str, benchmark_name: str) -> None:
    artifacts_dir = artifact_dir / "artifacts"

    capture_get(
        "job",
        benchmark_name,
        namespace,
        "yaml",
        artifacts_dir / "guidellm_benchmark_job.yaml",
    )
    capture_get(
        "pods",
        None,
        namespace,
        "yaml",
        artifacts_dir / "guidellm_benchmark_job.pods.yaml",
        selector=f"job-name={benchmark_name}",
    )

    logs_dir = artifacts_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    describe_dir = artifacts_dir / "describe"
    describe_dir.mkdir(parents=True, exist_ok=True)

    pod_names = _get_job_pod_names(namespace, benchmark_name)
    for pod_name in pod_names:
        oc(
            "logs",
            pod_name,
            "--all-containers",
            "-n",
            namespace,
            check=False,
            log_stdout=False,
            stdout_dest=logs_dir / f"guidellm_benchmark_{pod_name}.log",
        )
        oc(
            "describe",
            "pod",
            pod_name,
            "-n",
            namespace,
            check=False,
            log_stdout=False,
            stdout_dest=describe_dir / f"guidellm_benchmark_{pod_name}.describe.txt",
        )


def capture_get(
    kind: str,
    name: str | None,
    namespace: str,
    output: str,
    destination: Path,
    *,
    selector: str | None = None,
) -> None:
    args = ["get", kind]
    if name:
        args.append(name)
    args.extend(["-n", namespace])
    if selector:
        args.extend(["-l", selector])
    args.extend(["-o", output])
    oc(*args, check=False, log_stdout=False, stdout_dest=destination)


if __name__ == "__main__":
    run.main()
