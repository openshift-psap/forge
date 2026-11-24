import json
import logging
import os
import pathlib
import re
import signal
import sys
import threading
import time
import traceback
from datetime import datetime

import yaml

from projects.core.ci_entrypoint.prepare_ci import format_duration
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.dsl.utils.k8s import sanitize_k8s_name
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault
from projects.core.library.run_parallel import Parallel
from projects.core.notifications.send import send_notification
from projects.fournos_launcher.orchestration import job_management, pr_args, tunnelling
from projects.fournos_launcher.toolbox.submit_and_wait.main import (
    run as submit_and_wait,
)

logger = logging.getLogger(__name__)


def _signal_handler_sigint(sig, frame):
    """Handle SIGINT (Ctrl+C) for FOURNOS launcher."""
    print("\n🚫 FOURNOS launcher received SIGINT - shutting down jobs...")
    env.reset_artifact_dir()
    job_management.shutdown_fjobs_on_interrupt()
    sys.exit(137)


def _signal_handler_sigterm(sig, frame):
    """Handle SIGTERM for FOURNOS launcher."""
    print("\n🛑 FOURNOS launcher received SIGTERM - shutting down jobs...")
    env.reset_artifact_dir()
    job_management.shutdown_fjobs_on_interrupt()
    sys.exit(143)


def _cleanup_fjob(job_name):
    """Clean up a FournosJob, creating a notification if the MLFlow link is missing."""
    try:
        ctx = _cleanup_fjob(
            job_name=job_name,
            namespace=config.project.get_config("fournos.namespace"),
        )
        if ctx.missing_mlflow_link:
            ci_lib.add_notification_file(
                "missing-mlflow-link",
                f"FournosJob {job_name} preserved: no MLFlow link found in job status.",
            )
        else:
            logger.info(f"Cleaned up job: {job_name}")
    except Exception as cleanup_e:
        logger.warning(f"Failed to cleanup job {job_name}: {cleanup_e}")


def _setup_signal_handlers():
    """Set up signal handlers for FOURNOS job shutdown."""
    try:
        # Store original handlers
        original_sigint = signal.signal(signal.SIGINT, _signal_handler_sigint)
        original_sigterm = signal.signal(signal.SIGTERM, _signal_handler_sigterm)

        logger.debug("FOURNOS signal handlers installed")

        # Store references so we can restore them if needed
        _setup_signal_handlers._original_sigint = original_sigint
        _setup_signal_handlers._original_sigterm = original_sigterm

    except Exception as e:
        logger.warning(f"Failed to set up FOURNOS signal handlers: {e}")


def _format_operational_notifications(metadata_dir: pathlib.Path) -> str:
    """Format the CI operational notifications (000__ci_metadata/notifications/*.txt)
    as a markdown section for the completion notification.

    Returns an empty string when there is no notification file.
    """
    notifications_dir = metadata_dir / "notifications"
    if not notifications_dir.is_dir():
        return ""

    blocks = []
    for notification_file in sorted(notifications_dir.glob("*.txt")):
        try:
            content = notification_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning(f"Failed to read notification file {notification_file}: {e}")
            continue

        if not content:
            continue

        # same title formatting as projects.core.library.export_notifications
        title = notification_file.stem.replace("__", " ").replace("_", " ").title()
        title = re.sub(r"^\d+\s+", "", title)

        lines = [f"##### {title}"]
        lines += [f"> {line}" for line in content.splitlines()]
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    return "\n**Operational notifications:**\n\n" + "\n\n".join(blocks) + "\n"


def _register_notification_send_failure(job_type: str, reason: str) -> None:
    """Register a CI notification file when the GitHub notification could not be sent,
    so that the failure is visible in the CI artifacts instead of a single log line."""
    ci_lib.add_notification_file(
        name="github_notification_failed",
        message=(
            f"The GitHub completion notification for this {job_type} job could not be sent.\n\n"
            f"Reason: {reason}\n\n"
            "Check the 'github:' error lines in run.log for details."
        ),
    )


def _notification_warning_exit_status():
    """Exit status flagging, as a non-fatal warning, that the GitHub completion
    notification could not be sent (the CI run itself stays successful)."""
    return (
        0,
        ci_lib.ExitCategory.WARNING,
        "the GitHub completion notification could not be sent",
    )


def send_github_notification(
    success: bool,
    job_type: str = "single",
    start_time: float | None = None,
    error: Exception | None = None,
) -> bool:
    """Send a simplified GitHub notification with essential test information.

    Returns True if the notification was sent (or intentionally skipped),
    False if it could not be sent."""

    def get_ocpci_link(path, is_raw_file=False, base=None, is_dir=False):
        if base is None:
            base, suffix = get_ci_base_link(is_raw_file, is_dir)
        else:
            suffix = None

        link = base + (f"/{path}" if path else "") + (suffix if suffix else "")

        return link

    try:
        artifact_dir = pathlib.Path(env.ARTIFACT_DIR)

        # 1. Test status: green/red flag
        status_flag = "🟢" if success else "🔴"

        # 2. Get project and job configuration
        project_name = config.project.get_config("ci_job.project")
        job_args = config.project.get_config("ci_job.args")

        # 3. Link to test results (MLflow URL if available)
        test_results_link = None
        for job_file in artifact_dir.glob("**/fournos_jobs/*-final-status.yaml"):
            try:
                with open(job_file) as f:
                    status_data = yaml.safe_load(f)

                mlflow_info = (
                    status_data.get("status", {})
                    .get("engineStatus", {})
                    .get("forge", {})
                    .get("exportArtifacts", {})
                    .get("caliper_artifacts_export", {})
                    .get("backends", {})
                    .get("mlflow", {})
                )

                if (
                    mlflow_info
                    and mlflow_info.get("status") == "success"
                    and mlflow_info.get("run_url")
                ):
                    test_results_link = f"[Test Results]({mlflow_info['run_url']})"
                    break
            except Exception:
                continue

        if not test_results_link:
            test_results_link = "Test results not available"

        # Read test configuration from pr_config.txt
        pr_config_content = "Configuration not available"
        metadata_dir = ci_lib.get_ci_metadata_dir_location()
        pr_config_file = metadata_dir / "pr_config.txt"
        if pr_config_file.exists():
            try:
                with open(pr_config_file, encoding="utf-8") as f:
                    pr_config_content = f.read().strip()
            except Exception as e:
                logger.warning(f"Failed to read pr_config.txt: {e}")
                pr_config_content = f"Error reading configuration: {e}"

        # Get various CI links
        ocpci_results_link = get_ocpci_link("", is_raw_file=False, is_dir=True)
        execution_logs_link = get_ocpci_link("run.log", is_raw_file=True)

        # Collect individual task log files
        task_log_links = []
        for log_file in artifact_dir.glob("**/task_logs/*"):
            if log_file.is_file():
                rel_path = log_file.relative_to(artifact_dir)
                ci_link = get_ocpci_link(str(rel_path), is_raw_file=True)
                task_log_links.append(f"  - [{log_file.name}]({ci_link})")

        # Format task logs section
        if task_log_links:
            task_logs_section = "* Task logs:\n" + "\n".join(task_log_links)
        else:
            task_logs_dir_link = get_ocpci_link("task_logs", is_raw_file=False, is_dir=True)
            task_logs_section = f"* [Task logs]({task_logs_dir_link})"

        # Calculate duration if start_time is provided
        duration_str = ""
        if start_time is not None:
            end_time = time.time()
            duration_seconds = int(end_time - start_time)
            duration_str = f" after {format_duration(duration_seconds)}"

        # Build structured notification
        presets = " ".join(job_args) if job_args else ""
        status_verb = "succeeded" if success else "failed"

        # Include error details if provided
        error_section = ""
        if error is not None:
            # Extract clean error message from TaskExecutionError if needed
            if isinstance(error, TaskExecutionError) and error.original_exception:
                clean_error_name = type(error.original_exception).__name__
                clean_error_msg = str(error.original_exception)
                error_section = f"\n**Error:** `{clean_error_name}: {clean_error_msg}`\n"
            else:
                error_section = f"\n**Error:** `{type(error).__name__}: {str(error)}`\n"

        operational_notifications_section = _format_operational_notifications(metadata_dir)

        notification_status = f"""<details>
<summary>{status_flag} Submission of <code>{project_name} {presets}</code> {status_verb} <code>{duration_str.strip()}</code> {status_flag}</summary>

* [OCPCI Test results]({ocpci_results_link})
{task_logs_section}
* [Execution logs]({execution_logs_link})
* {test_results_link.replace("[Test Results]", "[MLFlow Test Results]")}

</details>
{operational_notifications_section}{error_section}
```
{pr_config_content}
```"""

        # Write simplified notification to file for notification system pickup
        try:
            notification_file = artifact_dir / "COMPLETION-NOTIFICATION.md"
            with open(notification_file, "w", encoding="utf-8") as f:
                f.write(notification_status)
            logger.info(f"Created notification content for {job_type} job")
        except Exception as file_error:
            logger.warning(f"Failed to write notification file: {file_error}")
            return False

        # Send notification through notification system
        try:
            notification_vault = config.project.get_config("notifications.vault.name")

            ok = send_notification(
                message=notification_status,
                github=True,
                dry_run=False,
                notification_vault=notification_vault,
            )
        except Exception as send_error:
            logger.warning(f"Failed to send notification: {send_error}")
            _register_notification_send_failure(
                job_type, f"{type(send_error).__name__}: {send_error}"
            )
            return False

        if ok:
            logger.info(f"Sent GitHub notification for {job_type} job")
            return True

        logger.warning(f"GitHub notification for {job_type} job returned failure")
        _register_notification_send_failure(job_type, "send_notification returned failure")
        return False

    except Exception as e:
        logger.warning(f"Failed to send GitHub notification: {e}")

    return False


# returns a tuple (base_link, link_suffix)
def get_ci_base_link(is_raw_file=False, is_dir=False):
    if os.environ.get("OPENSHIFT_CI") != "true":
        logger.warning(
            "Test not running from a well-known CI engine, cannot extract the artifacts link."
        )

        return "https://no_known_ci_engine/", "?no_ext=true"

    try:
        job_spec = json.loads(os.environ["JOB_SPEC"])
    except KeyError:
        logger.error("JOB_SPEC environment variable is not set")
        raise
    except json.JSONDecodeError as e:
        logger.error("Failed to parse JOB_SPEC as JSON: %s", e)
        raise
    test_name = os.environ["JOB_NAME_SAFE"]
    job = job_spec["job"]
    build_id = job_spec["buildid"]

    if job_spec["type"] == "periodic":
        link_path = f"logs/{job}/{build_id}"

    else:
        pull_number = job_spec["refs"]["pulls"][0]["number"]
        github_org = job_spec["refs"]["org"]
        github_repo = job_spec["refs"]["repo"]

        link_path = f"pr-logs/pull/{github_org}_{github_repo}/{pull_number}/{job}/{build_id}"

    hostname = os.environ["HOSTNAME"]
    test_path = f"{hostname.replace(test_name + '-', '', 1)}/artifacts"

    return (
        "https://gcs.ci.openshift.org/gcs/test-platform-results-public/"
        + link_path
        + f"/artifacts/{test_name}/{test_path}",
        "",
    )


def get_fournos_instance() -> str:
    """Return the configured Fournos instance, validated against the known ones."""
    instance = config.project.get_config("fournos.instance")
    if instance not in ("psap-mgmt", "intlab"):
        raise ValueError(
            f"Unknown fournos.instance: {instance!r} (expected 'psap-mgmt' or 'intlab')"
        )
    return instance


def vaults_to_init() -> list[str]:
    """Vaults required by the launcher, for the configured Fournos instance only.

    The vault of the other instance is not used, and may not be available in
    the CI environment running the launcher.
    """
    instance = get_fournos_instance()
    other_instance = "psap_mgmt" if instance == "intlab" else "intlab"
    other_instance_vault = config.project.get_config(f"fournos.{other_instance}.vault.name")

    return [v for v in config.project.get_config("vaults") if v != other_instance_vault]


def init():
    env.init()
    run.init()
    result = pr_args.apply_pr_directives()
    if result == "help":
        logger.info("Help was requested - exiting with code 0")
        exit(0)

    config.init(pathlib.Path(__file__).parent, apply_config_overrides=False)
    config.project.apply_config_overrides(ignore_not_found=True)
    config.project.filter_out_used_overrides()
    vault.init(vaults_to_init())

    prepare_env()


def prepare_env():
    instance = get_fournos_instance()

    if instance == "intlab":
        logger.info("Using intlab Fournos instance (via SSH tunnel)")
        tunnelling.open_tunnel()
    else:
        logger.info("Using psap-mgmt Fournos instance (direct)")
        kubeconfig_path = vault.get_vault_content_path(
            config.project.get_config("fournos.psap_mgmt.vault.name"),
            config.project.get_config("fournos.psap_mgmt.vault.key"),
        )
        os.environ["KUBECONFIG"] = str(kubeconfig_path)


def submit_job():
    # Capture start time for duration calculation
    start_time = time.time()

    try:
        # Set up signal handlers for graceful job shutdown on interruption
        _setup_signal_handlers()

        overrides = {}
        overrides.update(config.project.get_config("overrides"))
        overrides.update(config.project.get_config("extra_overrides"))

        # Build env dict from pass lists
        env_dict = {}
        env_pass_lists = config.project.get_config("fournos.job.env", print=False)
        for _, pass_list in (env_pass_lists or {}).items():
            for env_var in pass_list:
                if env_var in os.environ:
                    env_dict[env_var] = os.environ[env_var]

        foreign_testing_project = os.environ.get("FORGE_FOREIGN_TESTING_PROJECT")
        if foreign_testing_project:
            logger.info("Foreign testing project detected: %s", foreign_testing_project)
            for env_var_name in ("PULL_PULL_SHA", "PULL_NUMBER"):
                if env_var_name in env_dict:
                    env_dict[f"FORGE_FOREIGN_TESTING_{env_var_name}"] = env_dict.pop(env_var_name)

        # Add extra environment variables
        extra_env = config.project.get_config("fournos.job.extra_env", {}, print=False)
        env_dict.update(extra_env)

        # Update display name with project and args
        project_name = config.project.get_config("ci_job.project")
        job_args = config.project.get_config("ci_job.args")

        if foreign_testing_project:
            if project_name != "project_not_set":
                job_args = [project_name, *job_args]
            project_name = foreign_testing_project
            config.project.set_config("ci_job.project", project_name)
            config.project.set_config("ci_job.args", job_args)

        if project_name == "project_not_set":
            raise RuntimeError("Forge project isn't configured. Cannot submit the Fjob")

        # job_args is always a list, format accordingly
        args_str = " ".join(job_args)

        display_name = f"{project_name} {args_str}".strip()
        config.project.set_config("fournos.job.display_name", display_name)
        logger.info(f"Set job display name: {display_name}")

        # Validate required configuration before job submission
        cluster_name = config.project.get_config("cluster.name")
        clusterless_mode = config.project.get_config("fournos.job.clusterless")

        if not cluster_name and not clusterless_mode:
            raise ValueError(
                "/cluster (cluster.name) must be set - cannot submit job without target cluster (unless /clusterless is used)"
            )

        # Get GPU hardware configuration
        gpu_count = config.project.get_config("fournos.job.hardware.gpu_count")
        gpu_type = config.project.get_config("fournos.job.hardware.gpu_type")

        # Validate GPU configuration - both must be present or both must be missing
        gpu_config_present = (gpu_count is not None, gpu_type is not None)
        if gpu_config_present[0] != gpu_config_present[1]:
            raise ValueError(
                "GPU configuration invalid: both gpu_count and gpu_type must be specified together, "
                f"or both must be null. Got gpu_count={gpu_count}, gpu_type={gpu_type}"
            )

        # Check if parallel jobs are configured
        parallel_jobs = config.project.get_config("fournos_launcher.parallel_jobs", {}, print=False)
        parallel_job_configs = []

        for idx, job_args_list in parallel_jobs.items():
            if job_args_list:  # Non-empty list
                parallel_job_configs.append((idx, job_args_list))

        # Prepare common submit_and_wait arguments
        submit_kwargs = {
            "cluster_name": cluster_name,
            "project": config.project.get_config("ci_job.project"),
            "variables_overrides": overrides,
            "namespace": config.project.get_config("fournos.namespace"),
            "owner": config.project.get_config("fournos.job.owner"),
            "pipeline_name": config.project.get_config("fournos.job.pipeline_name"),
            "env": env_dict,
            "ci_label": config.project.get_config("fournos.job.ci_label"),
            "exclusive": config.project.get_config("fournos.job.exclusive"),
            "clusterless": config.project.get_config("fournos.job.clusterless"),
            "gpu_count": gpu_count,
            "gpu_type": gpu_type,
            "ttl": config.project.get_config("fournos.job.ttl"),
        }

        if parallel_job_configs:
            logger.info(
                f"Found {len(parallel_job_configs)} parallel job configurations, launching in parallel"
            )

            # Generate timestamp for parallel job names (shared across all parallel jobs)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            raw_name = f"forge-{project_name}-{timestamp}"
            raw_name = sanitize_k8s_name(raw_name)

            # Track failures and job names across parallel jobs
            failure_lock = threading.Lock()
            has_failures = [False]  # Use list for mutable reference
            submitted_job_names = []
            submitted_job_lock = threading.Lock()

            def submit_parallel_job(job_index, job_args_list):
                """Submit a single parallel job with specific args."""
                # Combine base ci_job.args with parallel job-specific args
                base_args = config.project.get_config("ci_job.args")
                combined_args = base_args + job_args_list

                logger.info(f"Submitting parallel job {job_index} with args: {combined_args}")

                # Create display name consistent with single job format
                args_str = " ".join(combined_args)
                parallel_display_name = f"{project_name} {args_str} (job {job_index})".strip()

                # Create unique job name with timestamp and index
                unique_job_name = sanitize_k8s_name(f"{raw_name}-{job_index}")

                try:
                    # Track the job name for cleanup (job gets submitted even if waiting fails)
                    with submitted_job_lock:
                        submitted_job_names.append(unique_job_name)

                    # Create job-specific status directory
                    job_status_dest = env.ARTIFACT_DIR / unique_job_name
                    job_status_dest.mkdir(parents=True, exist_ok=True)

                    submit_and_wait(
                        **submit_kwargs,
                        args=combined_args,
                        display_name=parallel_display_name,
                        job_name=unique_job_name,
                        artifact_dirname_suffix=str(job_index),
                        status_dest=job_status_dest,
                    )
                    logger.info(f"Parallel job {job_index} completed successfully")
                except Exception as e:
                    logger.error(f"Parallel job {job_index} failed: {e}")
                    traceback.print_exc()

                    # Register failure in thread-safe way
                    with failure_lock:
                        has_failures[0] = True

            # Submit all parallel jobs with exit_on_exception=False to let others complete
            with Parallel("parallel_jobs", exit_on_exception=False) as parallel:
                for job_index, job_args_list in parallel_job_configs:
                    parallel.delayed(submit_parallel_job, job_index, job_args_list)

            # Cleanup all submitted jobs
            if submitted_job_names:
                logger.info(
                    f"Cleaning up {len(submitted_job_names)} parallel jobs: {', '.join(submitted_job_names)}"
                )
                for job_name in submitted_job_names:
                    _cleanup_fjob(job_name)

            # Send simplified GitHub notification instead of generating detailed files
            notification_sent = send_github_notification(
                success=not has_failures[0], job_type="parallel", start_time=start_time
            )

            # Check if any jobs failed
            if has_failures[0]:
                logger.error("One or more parallel jobs failed")
                return 1

            logger.info("All parallel jobs completed successfully")

            if not notification_sent:
                return _notification_warning_exit_status()

            return 0
        else:
            # No parallel jobs configured, run single job as before
            logger.info("No parallel jobs configured, running single job")

            # Generate unique job name for single job
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            single_job_name = sanitize_k8s_name(f"forge-{project_name}-{timestamp}")

            try:
                submit_and_wait(
                    **submit_kwargs,
                    args=config.project.get_config("ci_job.args"),
                    display_name=config.project.get_config("fournos.job.display_name"),
                    job_name=single_job_name,
                    status_dest=env.ARTIFACT_DIR,
                )
                logger.info("Single job completed successfully")
                return_code = 0
            except Exception as e:
                logger.error(f"Single job failed: {e}")
                return_code = 1
                job_error = e
            else:
                job_error = None

            # Send simplified GitHub notification
            notification_sent = send_github_notification(
                success=(return_code == 0),
                job_type="single",
                start_time=start_time,
                error=job_error,
            )

            _cleanup_fjob(single_job_name)

            if return_code == 0 and not notification_sent:
                return _notification_warning_exit_status()

            return return_code

    except Exception as e:
        logger.error(f"Submit job failed with exception: {e}")

        # Send notification with error details
        send_github_notification(success=False, job_type="single", start_time=start_time, error=e)

        # Re-raise the exception for proper error handling by the CI system
        raise
