#!/usr/bin/env python3

"""
FOURNOS job submission and monitoring using task-based DSL
Parameters are passed from the entrypoint that reads the configuration
"""

import logging
from datetime import datetime
from pathlib import Path

import yaml

from projects.core.dsl import (
    always,
    entrypoint,
    execute_tasks,
    retry,
    shell,
    task,
    template,
)
from projects.core.dsl.utils.k8s import is_valid_k8s_name, sanitize_k8s_name
from projects.core.library import env as env_mod

logger = logging.getLogger(__name__)


class FournosJobFailureError(RuntimeError):
    """Custom exception for FOURNOS job failures."""

    def __init__(
        self, job_name: str, failure_message: str, namespace: str, error_type: str = "failure"
    ):
        self.job_name = job_name
        self.failure_message = failure_message
        self.namespace = namespace
        self.error_type = error_type

        if error_type == "shutdown":
            msg = f"FOURNOS Job '{job_name}' was shutdown: {failure_message}"
        else:
            msg = f"FOURNOS Job '{job_name}' failed: {failure_message}"

        super().__init__(msg)


@entrypoint
def run(
    cluster_name: str,
    project: str,
    *,
    args: list = None,
    variables_overrides: dict = None,
    job_name: str = "",
    namespace: str = "fournos-jobs",
    owner: str = "",
    display_name: str = "",
    pipeline_name: str = "",
    env: dict = None,
    status_dest=None,
    ci_label: str = None,
    exclusive: bool = True,
    gpu_count: int = None,
    gpu_type: str = None,
):
    """
    Submit a FOURNOS job and wait for completion

    Args:
        cluster_name: Name of the target cluster for the FOURNOS job
        project: The project name to execute (e.g., 'llm_d', 'skeleton')
        args: List of arguments to pass to the project (default: empty list)
        variables_overrides: Dictionary of config variables to override (default: empty dict)
        job_name: Custom name for the FOURNOS job (auto-generated if empty)
        namespace: Kubernetes namespace for the FOURNOS job (default: "fournos-jobs")
        owner: Owner of the FOURNOS job (default: empty)
        display_name: Human-readable display name for the job (default: empty)
        pipeline_name: Name of the pipeline to execute (default: empty)
        env: Dictionary of environment variables to set (default: empty dict)
        status_dest: Directory to save status information and pod logs (default: {artifact_dir}/artifacts)
        ci_label: CI run label for tracking and cancellation (default: None)
        exclusive: Whether the job should run exclusively on its nodes (default: True)
        gpu_count: Number of GPUs required for the job (default: None)
        gpu_type: Type of GPU required for the job (default: None)

    Examples:
        # Called by entrypoint with config values:
        run(
            cluster_name="my-cluster",
            project="llm_d",
            args=["test", "--verbose"],
            variables_overrides={"model": "mistral", "replicas": 2},
            namespace="my-fournos-jobs",
            owner="user@example.com",
            display_name="LLM Testing Job",
            pipeline_name="test-pipeline",
            env={"DEBUG": "1", "LOG_LEVEL": "info"},
            status_dest="/path/to/artifacts",
            gpu_count=2,
            gpu_type="nvidia-tesla-v100",
        )
    """
    # Set defaults
    if args is None:
        args = []
    if variables_overrides is None:
        variables_overrides = {}
    if env is None:
        env = {}
    if status_dest is None:
        status_dest = env_mod.ARTIFACT_DIR / "artifacts"
    else:
        status_dest = Path(status_dest)
        if not status_dest.exists():
            raise ValueError(f"status_dest='{status_dest}' does not exist")

    # Execute all registered tasks in order
    return execute_tasks(locals())


@task
def validate_inputs(args, ctx):
    """Validate input parameters"""

    if not args.cluster_name:
        raise ValueError("cluster_name is required")

    if not args.project:
        raise ValueError("project is required")

    if not isinstance(args.args, list):
        raise ValueError("args should be a list")

    if not isinstance(args.variables_overrides, dict):
        raise ValueError("variables_overrides should be a dict")

    if not isinstance(args.env, dict):
        raise ValueError("env should be a dict")

    return "Inputs validated"


@task
def setup_directories(args, context):
    """Create the artifacts directory"""

    shell.mkdir("artifacts")
    return "Artifacts directory created"


@task
def generate_job_name(args, ctx):
    """Generate job name if not provided and ensure K8s compatibility"""

    if args.job_name:
        # Validate that user-provided job name is already normalized
        if not is_valid_k8s_name(args.job_name):
            normalized_name = sanitize_k8s_name(args.job_name)
            raise ValueError(
                f"Job name '{args.job_name}' is not valid for Kubernetes. "
                f"Please use the normalized name: '{normalized_name}'"
            )
        ctx.final_job_name = args.job_name
    else:
        # Generate and sanitize auto job name
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        raw_name = f"forge-{args.project}-{timestamp}"
        ctx.final_job_name = sanitize_k8s_name(raw_name)

    return f"Job name: {ctx.final_job_name}"


@task
def ensure_oc(args, ctx):
    """Ensure oc is available and connected"""

    shell.run("which oc || (echo 'oc not found in PATH' && exit 1)")
    shell.run("oc whoami")


@task
def create_job_manifest(args, ctx):
    """Create FOURNOS job manifest"""

    # Render job manifest from template
    ctx.manifest_file = args.artifact_dir / "src" / f"{ctx.final_job_name}-manifest.yaml"
    shell.mkdir(ctx.manifest_file.parent)

    template.render_template_to_file("job.yaml.j2", ctx.manifest_file)

    return f"Job manifest created: {ctx.manifest_file}"


@task
def submit_fournos_job(args, ctx):
    """Submit the FOURNOS job"""

    # Apply the job manifest (will raise CalledProcessError with full details on failure)
    shell.run(f"oc apply -f {ctx.manifest_file}")

    return f"Successfully submitted FOURNOS job: {ctx.final_job_name}"


@retry(attempts=360, delay=10, backoff=1.0)
@task
def wait_for_job_completion(args, ctx):
    """Wait for FOURNOS job to complete"""

    # Check job status
    status_result = shell.run(
        f'oc get fournosjob {ctx.final_job_name} -n {args.namespace} -o jsonpath="{{.status.phase}}"',
        check=False,
    )

    if not status_result.success:
        # Check if it's a "not found" error (permanent failure) vs temporary error
        if "not found" in status_result.stderr.lower():
            raise FournosJobFailureError(
                ctx.final_job_name,
                "Job not found - may have been deleted or never created",
                args.namespace,
                "not_found",
            )

        # Other errors might be temporary, retry
        logger.info(
            f"Failed to get job status, retrying... (stderr: {status_result.stderr.strip()})"
        )
        return False  # Retry

    status = status_result.stdout.strip()

    # Check if shutdown has been requested
    shutdown_result = shell.run(
        f'oc get fournosjob {ctx.final_job_name} -n {args.namespace} -o jsonpath="{{.spec.shutdown}}"',
        check=False,
    )

    if shutdown_result.success and shutdown_result.stdout.strip():
        shutdown_value = shutdown_result.stdout.strip()
        logger.warning(
            f"Job {ctx.final_job_name} has spec.shutdown={shutdown_value} - aborting wait"
        )
        raise FournosJobFailureError(
            ctx.final_job_name, f"spec.shutdown={shutdown_value}", args.namespace, "shutdown"
        )

    if status in ["Succeeded"]:
        return f"Job {ctx.final_job_name} completed successfully"

    elif status == "Failed":
        # Get failure details
        failure_result = shell.run(
            f'oc get fournosjob {ctx.final_job_name} -n {args.namespace} -o jsonpath="{{.status.message}}"',
            check=False,
        )
        failure_msg = failure_result.stdout.strip() if failure_result.success else "Unknown failure"
        raise FournosJobFailureError(
            ctx.final_job_name, failure_msg, args.namespace
        )  # Abort on failure
    elif status in ["Running", "Pending", "Admitted"]:
        logger.info(f"Job {ctx.final_job_name} status: {status}. Keep waiting.")
        return False  # Retry
    else:
        logger.info(f"Job {ctx.final_job_name} status: {status}")
        return False  # Unknown status, retry


@always
@task
def capture_final_job_status(args, ctx):
    """Capture final job status and details"""
    # Guard: Check if job name was generated (might not exist if early validation failed)
    if not hasattr(ctx, "final_job_name"):
        return "No job name available - skipping status capture"

    # Save to fournos_jobs directory for notification pickup
    fournos_jobs_dir = args.artifact_dir / "fournos_jobs"
    shell.mkdir(fournos_jobs_dir)
    job_file = fournos_jobs_dir / f"{ctx.final_job_name}-final-status.yaml"

    # Get full job details
    result = shell.run(
        f"oc get fournosjob {ctx.final_job_name} -n {args.namespace} -o yaml",
        stdout_dest=job_file,
        check=False,
    )

    if not result.success:
        return f"Failed to capture job status: {result.stderr or 'Unknown error'}"

    # Clean up annotations from the saved YAML
    try:
        with open(job_file) as f:
            job_data = yaml.safe_load(f)

        # Remove unwanted annotations from the job
        annotations_to_remove = [
            "kopf.zalando.org/last-handled-configuration",
            "kubectl.kubernetes.io/last-applied-configuration",
        ]

        if job_data and job_data.get("metadata", {}).get("annotations"):
            for annotation in annotations_to_remove:
                job_data["metadata"]["annotations"].pop(annotation, None)

            # Remove empty annotations dict if it becomes empty
            if not job_data["metadata"]["annotations"]:
                del job_data["metadata"]["annotations"]

        # Write cleaned YAML back to file
        with open(job_file, "w") as f:
            yaml.safe_dump(job_data, f, default_flow_style=False, sort_keys=False)

    except Exception as e:
        logger.warning(f"Failed to clean annotations from job status: {e}")

    return f"Final job status captured to {ctx.final_job_name}-final-status.yaml"


@always
@task
def capture_pod_information(args, ctx):
    """Capture pod status and logs for the FOURNOS job"""

    # Guard: Check if job name was generated (might not exist if early validation failed)
    if not hasattr(ctx, "final_job_name"):
        return "No job name available - skipping pod information capture"

    # List all pods with the FOURNOS job label
    label_selector = f"fournos.dev/job-name={ctx.final_job_name}"

    # Get pod status and save to status destination
    shell.run(
        f"oc get pods -l {label_selector} -n {args.namespace}",
        stdout_dest=args.status_dest / "tasks.status",
        check=False,
    )

    # Get list of pod names to collect logs
    pod_list_result = shell.run(
        f'oc get pods -l {label_selector} -n {args.namespace} -o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
    )

    if not (pod_list_result.success and pod_list_result.stdout.strip()):
        return "No pods found for the job or failed to get pod list"

    pod_names = pod_list_result.stdout.strip().split()

    # Create logs directory in status destination
    logs_dir = args.status_dest / "task_logs"
    shell.mkdir(logs_dir)

    for pod_name in pod_names:
        # Get logs for each pod (default container only)
        shell.run(
            f"oc logs {pod_name} -n {args.namespace}",
            stdout_dest=logs_dir / f"{pod_name}.log",
            check=False,
        )
    return f"Captured logs for {len(pod_names)} pods in logs/ directory"


@always
@task
def capture_pipelinerun_specs(args, ctx):
    """
    Persist each Tekton ``PipelineRun`` (``pr``) ``.spec`` for runs labeled with
    ``fournos.dev/job-name=<job>`` (e.g. ``fournos.dev/job-name=forge-skeleton-20260424-090246``),
    before the FournosJob is deleted.
    """
    if not hasattr(ctx, "final_job_name"):
        return "No job name available — skipping PipelineRun spec capture"

    artifact_dir = args.artifact_dir / "artifacts"
    shell.mkdir(str(artifact_dir))

    fj_name = ctx.final_job_name
    ns = args.namespace
    # Same label the workload uses to associate PipelineRuns with this Fournos job
    label = f"fournos.dev/job-name={fj_name}"

    yaml_list = shell.run(
        f"oc get pipelinerun -n {ns} -l {label!r} -oyaml",
        check=False,
        stdout_dest=artifact_dir / "pipelinerun.yaml",
    )
    if not (yaml_list.success and yaml_list.stdout and yaml_list.stdout.strip()):
        return f"No PipelineRun query result (label {label!r}): {yaml_list.stderr or yaml_list.stdout or 'empty'}"

    return f"Wrote the PipelineRun spec file under {artifact_dir} (label {label})"


@always
@task
def capture_pod_specs(args, ctx):
    """
    Persist each workload Pod ``.spec`` for pods labeled
    ``fournos.dev/job-name=<job>`` (e.g. ``fournos.dev/job-name=forge-skeleton-20260424-090246``),
    before the FournosJob is deleted.
    """
    if not hasattr(ctx, "final_job_name"):
        return "No job name available — skipping pod spec capture"

    artifact_dir = args.artifact_dir / "artifacts"
    shell.mkdir(str(artifact_dir))

    fj_name = ctx.final_job_name
    ns = args.namespace
    label = f"fournos.dev/job-name={fj_name}"

    pods_file = artifact_dir / "pods.yaml"
    yaml_list = shell.run(
        f"oc get pods -l {label!r} -n {ns} -oyaml",
        check=False,
        stdout_dest=pods_file,
    )
    if not (yaml_list.success and yaml_list.stdout and yaml_list.stdout.strip()):
        return f"No pods for label {label!r} (or list failed): {yaml_list.stderr or ''}".strip()

    return f"Wrote the pod spec file under {artifact_dir} (label {label})"


if __name__ == "__main__":
    run.main()
