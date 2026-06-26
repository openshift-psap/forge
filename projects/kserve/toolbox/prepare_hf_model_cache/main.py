#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from projects.core.dsl import always, entrypoint, execute_tasks, retry, shell, task
from projects.core.dsl.utils import (
    slugify_identifier,
    truncate_k8s_name,
    write_json,
    write_text,
    write_yaml,
)
from projects.core.dsl.utils.k8s import (
    oc,
    oc_apply,
    oc_get_json,
    oc_resource_exists,
)
from projects.kserve.toolbox.prepare_hf_model_cache.utils import (
    annotate_model_cache_pvc,
    job_pod_names,
    model_cache_pvc_ready,
    pvc_access_mode_matches,
    render_hf_model_cache_job,
    render_model_cache_pvc,
)

logger = logging.getLogger("TOOLBOX")


@entrypoint
def run(
    *,
    namespace: str,
    namespace_is_managed: bool,
    model_key: str,
    model_uri: str,
    pvc_size: str,
    access_mode: str,
    storage_class_name: str | None,
    pvc_name_prefix: str,
    model_directory_name: str,
    marker_filename: str = "cached.marker",
    wait_timeout_seconds: int = 3600,
    poll_interval_seconds: int = 30,
    downloader_image: str,
    hf_token_file_path: str | None,
    pod_image_pull_policy: str = "IfNotPresent",
) -> int:
    """Prepare a HuggingFace model cache PVC and populate it when needed.

    Args:
        namespace: Namespace where the workload runs
        namespace_is_managed: Whether namespace lifecycle is managed by llm_d
        model_key: Selected model key
        model_uri: HuggingFace model URI (hf://...)
        pvc_size: Size of the PVC to create
        access_mode: PVC access mode
        storage_class_name: Storage class for the PVC
        pvc_name_prefix: Prefix for generated PVC names
        model_directory_name: Directory name within PVC for model files
        marker_filename: Name of the cache marker file
        wait_timeout_seconds: Timeout for download job completion
        poll_interval_seconds: Polling interval for job status
        downloader_image: Container image for HuggingFace downloads
        hf_token_file_path: Path to file containing HF token (optional)
        pod_image_pull_policy: Image pull policy for download pods

    Note:
        If hf_token_file_path is provided, a K8s secret will be created from the file content.
        If not provided, download will proceed unauthenticated with a warning.
    """

    execute_tasks(locals())
    return 0


@task
def build_cache_spec(args, ctx):
    """Build the model cache specification"""

    if not args.model_uri.startswith("hf://"):
        raise ValueError(f"Expected HF model URI, got: {args.model_uri}")

    # Build model cache spec
    cache_key = hashlib.sha256(args.model_uri.encode("utf-8")).hexdigest()[:10]
    pvc_name = truncate_k8s_name(
        f"{args.pvc_name_prefix}-{slugify_identifier(args.model_key, max_length=32)}-{cache_key}"
    )

    cache_spec = {
        "source_uri": args.model_uri,
        "source_scheme": "hf",
        "cache_key": cache_key,
        "namespace": args.namespace,
        "pvc_name": pvc_name,
        "pvc_size": args.pvc_size,
        "access_mode": args.access_mode,
        "storage_class_name": args.storage_class_name,
        "model_path": args.model_directory_name,
        "model_uri": f"pvc://{pvc_name}/{args.model_directory_name}",
        "marker_filename": args.marker_filename,
        "marker_path": f"/cache/{args.model_directory_name}/{args.marker_filename}",
        "download_job_name": truncate_k8s_name(f"{pvc_name}-download"),
        "hf_token_secret_name": None,  # Will be set dynamically from vault
        "hf_token_secret_key": "token",
        "oci_image_path": None,
        "oci_registry_auth_secret_name": None,
        "oci_registry_auth_secret_key": None,
    }

    if args.namespace_is_managed:
        logger.warning(
            "Model cache PVC %s lives in managed namespace %s. Namespace cleanup will remove it; cache reuse requires a stable namespace override.",
            cache_spec["pvc_name"],
            cache_spec["namespace"],
        )

    ctx.cache_spec = cache_spec
    ctx.hf_secret_created = False
    ctx.hf_secret_name = None
    ctx.pvc_exists = False
    return f"Cache spec built for {cache_spec['pvc_name']}"


@task
def ensure_pvc(args, ctx):
    """Ensure the model cache PVC exists"""

    cache_spec = ctx.cache_spec
    existing = oc_get_json(
        "persistentvolumeclaim",
        name=cache_spec["pvc_name"],
        namespace=cache_spec["namespace"],
        ignore_not_found=True,
    )

    if existing:
        actual_modes = existing.get("spec", {}).get("accessModes", [])
        if not pvc_access_mode_matches(actual_modes, cache_spec["access_mode"]):
            raise RuntimeError(
                f"PVC {cache_spec['pvc_name']} exists with access modes {actual_modes}, expected {cache_spec['access_mode']}"
            )

        actual_storage_class = existing.get("spec", {}).get("storageClassName")
        if (
            cache_spec["storage_class_name"]
            and actual_storage_class != cache_spec["storage_class_name"]
        ):
            raise RuntimeError(
                f"PVC {cache_spec['pvc_name']} exists with storageClassName={actual_storage_class}, expected {cache_spec['storage_class_name']}"
            )

        # Store the fact that PVC already existed
        ctx.pvc_exists = True
        return f"PVC {cache_spec['pvc_name']} already exists"

    # Ensure the src directory exists
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    pvc_manifest = render_model_cache_pvc(cache_spec)
    oc_apply(src_dir / "model-cache-pvc.yaml", pvc_manifest)

    # Save manifest to artifacts
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(artifacts_dir / "pvc-manifest.yaml", pvc_manifest)

    # Store the fact that PVC was created
    ctx.pvc_exists = True
    return f"PVC {cache_spec['pvc_name']} created"


@task
def create_hf_token_secret(args, ctx):
    """Create HuggingFace token secret from file (if provided)"""

    cache_spec = ctx.cache_spec

    if not args.hf_token_file_path:
        logger.warning("No HF token file provided - proceeding with unauthenticated download")
        return "No HF token file - will download unauthenticated"

    token_file = Path(args.hf_token_file_path)
    if not token_file.exists():
        raise FileNotFoundError(f"HF token file does not exist: {args.hf_token_file_path}")

    # Create a unique secret name to avoid conflicts
    secret_name = f"{cache_spec['pvc_name']}-hf-token"

    # Delete existing secret if it exists to avoid conflicts
    oc(
        "delete",
        "secret",
        secret_name,
        "-n",
        cache_spec["namespace"],
        "--ignore-not-found=true",
        check=False,
    )

    # Create the secret using --from-file
    oc(
        "create",
        "secret",
        "generic",
        secret_name,
        f"--from-file=token={args.hf_token_file_path}",
        "-n",
        cache_spec["namespace"],
    )

    # Store secret info in context for later use and cleanup
    ctx.hf_secret_created = True
    ctx.hf_secret_name = secret_name
    logger.info(f"Created HF token secret: {secret_name}")
    return f"HF token secret {secret_name} created from file"


@task
def check_cache_ready(args, ctx):
    """Check if the cache is already populated"""

    cache_spec = ctx.cache_spec
    if model_cache_pvc_ready(cache_spec):
        logger.info(
            "Model cache PVC %s already contains %s; skipping download",
            cache_spec["pvc_name"],
            cache_spec["source_uri"],
        )
        ctx.cache_ready = True
        return f"Cache already populated in {cache_spec['pvc_name']}"

    ctx.cache_ready = False
    return f"Cache {cache_spec['pvc_name']} needs population"


@task
def delete_existing_job(args, ctx):
    """Delete any existing download job"""

    cache_spec = ctx.cache_spec
    oc(
        "delete",
        "job",
        cache_spec["download_job_name"],
        "-n",
        cache_spec["namespace"],
        "--ignore-not-found=true",
        check=False,
    )
    return f"Initiated deletion of job {cache_spec['download_job_name']}"


@retry(attempts=24, delay=5, backoff=1.0)
@task
def wait_job_deleted(args, ctx):
    """Wait for job deletion to complete"""

    cache_spec = ctx.cache_spec
    if not oc_resource_exists(
        "job", cache_spec["download_job_name"], namespace=cache_spec["namespace"]
    ):
        return f"Job {cache_spec['download_job_name']} deleted"
    return False  # Retry


@task
def create_download_job(args, ctx):
    """Create the model download job"""

    if getattr(ctx, "cache_ready", False):
        return "Skipping job creation - cache already ready"

    cache_spec = ctx.cache_spec

    # Ensure the src directory exists
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Use the created secret if available
    effective_secret_name = ctx.hf_secret_name if ctx.hf_secret_created else None
    job_manifest = render_hf_model_cache_job(args, cache_spec, effective_secret_name)
    oc_apply(src_dir / "model-cache-job.yaml", job_manifest)

    # Save manifest to artifacts (not logs)
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(artifacts_dir / "job-manifest.yaml", job_manifest)

    return f"Download job {cache_spec['download_job_name']} created"


@retry(attempts=30, delay=30, backoff=1.0)
@task
def wait_for_download(args, ctx):
    """Wait for the download job to complete"""

    if getattr(ctx, "cache_ready", False):
        return "Skipping download wait - cache already ready"

    cache_spec = ctx.cache_spec
    job_name = cache_spec["download_job_name"]
    namespace = cache_spec["namespace"]

    # Check job status
    payload = oc_get_json(
        "job",
        name=job_name,
        namespace=namespace,
        ignore_not_found=True,
    )
    if not payload:
        raise RuntimeError(
            f"Download job {job_name} not found - job may have been externally deleted"
        )

    status = payload.get("status", {})

    # Check if succeeded
    if status.get("succeeded", 0):
        return f"Download job {job_name} completed successfully"

    # Check if failed
    failed_count = status.get("failed", 0)
    for condition in status.get("conditions", []):
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            raise RuntimeError(
                f"job/{job_name} failed: {condition.get('reason') or 'unknown reason'}"
            )
    if failed_count:
        raise RuntimeError(f"job/{job_name} failed after {failed_count} attempt(s)")

    # Still running - show pod status
    shell.run(
        f"oc get pods -n {namespace} -l job-name={job_name}",
        check=False,
    )

    return (False, f"Download job {job_name} still running, retrying...")


@task
def capture_download_artifacts(args, ctx):
    """Capture download job artifacts (logs, pod YAML, etc.)"""

    cache_spec = ctx.cache_spec
    artifact_dir = args.artifact_dir / "artifacts" / "model-cache"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Save spec
    write_json(
        artifact_dir / "spec.json",
        {
            "pvc_name": cache_spec["pvc_name"],
            "model_uri": cache_spec["model_uri"],
            "source_uri": cache_spec["source_uri"],
            "source_scheme": cache_spec["source_scheme"],
        },
    )

    # Capture PVC YAML to file (not logs)
    pvc_result = oc(
        "get",
        "persistentvolumeclaim",
        cache_spec["pvc_name"],
        "-n",
        cache_spec["namespace"],
        "-o",
        "yaml",
        "--ignore-not-found=true",
        check=False,
        log_stdout=False,
    )
    if pvc_result.returncode == 0 and pvc_result.stdout:
        write_text(artifact_dir / "pvc.yaml", pvc_result.stdout)

    # Capture Job YAML to file (not logs)
    job_result = oc(
        "get",
        "job",
        cache_spec["download_job_name"],
        "-n",
        cache_spec["namespace"],
        "-o",
        "yaml",
        "--ignore-not-found=true",
        check=False,
        log_stdout=False,
    )
    if job_result.returncode == 0 and job_result.stdout:
        write_text(artifact_dir / "job.yaml", job_result.stdout)

    # Capture pod logs and YAML to files (not console)
    for pod_name in job_pod_names(cache_spec["download_job_name"], cache_spec["namespace"]):
        # Get pod YAML to file
        pod_result = oc(
            "get",
            "pod",
            pod_name,
            "-n",
            cache_spec["namespace"],
            "-o",
            "yaml",
            "--ignore-not-found=true",
            check=False,
            log_stdout=False,
        )
        if pod_result.returncode == 0 and pod_result.stdout:
            write_text(artifact_dir / f"{pod_name}.yaml", pod_result.stdout)

        # Get pod logs to file
        log_result = oc(
            "logs",
            pod_name,
            "-n",
            cache_spec["namespace"],
            check=False,
            log_stdout=False,
        )
        if log_result.returncode == 0 and log_result.stdout:
            write_text(artifact_dir / f"{pod_name}.log", log_result.stdout)

    return f"Artifacts captured for {cache_spec['pvc_name']}"


@task
def finalize_cache(args, ctx):
    """Finalize the cache by annotating and labeling the PVC"""

    if getattr(ctx, "cache_ready", False):
        return f"Cache finalized for existing {ctx.cache_spec['pvc_name']}"

    cache_spec = ctx.cache_spec
    annotate_model_cache_pvc(cache_spec)
    return f"Cache finalized and labeled as populated for {cache_spec['pvc_name']}"


@always
@task
def cleanup_download_job(args, ctx):
    """Clean up the completed download job and its pods"""

    if getattr(ctx, "cache_ready", False):
        return "No download job to clean up - cache was already ready"

    cache_spec = ctx.cache_spec

    # Delete the download job (this will also delete associated pods)
    oc(
        "delete",
        "job",
        cache_spec["download_job_name"],
        "-n",
        cache_spec["namespace"],
        "--ignore-not-found=true",
        check=False,
        log_stdout=False,
    )

    logger.info(f"Cleaned up download job: {cache_spec['download_job_name']}")
    return f"Download job {cache_spec['download_job_name']} deleted"


@always
@task
def cleanup_hf_token_secret(args, ctx):
    """Clean up the HuggingFace token secret if we created it"""

    if not ctx.hf_secret_created or not ctx.hf_secret_name:
        return "No HF token secret to clean up"

    cache_spec = ctx.cache_spec

    # Delete the secret we created
    oc(
        "delete",
        "secret",
        ctx.hf_secret_name,
        "-n",
        cache_spec["namespace"],
        "--ignore-not-found=true",
        check=False,
        log_stdout=False,
    )

    logger.info(f"Cleaned up HF token secret: {ctx.hf_secret_name}")
    return f"HF token secret {ctx.hf_secret_name} deleted"


if __name__ == "__main__":
    run.main()
