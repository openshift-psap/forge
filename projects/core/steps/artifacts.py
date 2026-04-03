"""Artifact collection step - shared by all projects."""

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from projects.core.workflow import StepResult, WorkflowStep

if TYPE_CHECKING:
    from projects.core.workflow import WorkflowContext

logger = logging.getLogger(__name__)


class CollectArtifactsStep(WorkflowStep):
    """
    Collect logs, events, and pod status for debugging.

    Always runs as a finally step to capture artifacts regardless
    of success or failure. Does not fail the workflow if collection
    fails - just logs warnings.

    Can be customized per project:
        - rhaiis: app_label="vllm"
        - llm_d: app_label="epp"
    """

    def __init__(
        self,
        app_label: str = "vllm",
        namespace: str | None = None,
        collect_events: bool = True,
        collect_pod_logs: bool = True,
        collect_pod_describe: bool = True,
        name: str | None = None,
    ):
        """
        Initialize artifact collection step.

        Args:
            app_label: Kubernetes app label to filter pods (e.g., "vllm", "epp")
            namespace: Kubernetes namespace (uses current context if None)
            collect_events: Whether to collect namespace events
            collect_pod_logs: Whether to collect pod logs
            collect_pod_describe: Whether to collect pod descriptions
            name: Optional step name
        """
        super().__init__(name=name or "collect_artifacts")
        self.app_label = app_label
        self.namespace = namespace
        self.collect_events = collect_events
        self.collect_pod_logs = collect_pod_logs
        self.collect_pod_describe = collect_pod_describe

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Collect artifacts from cluster."""
        step_dir = ctx.artifact_dir / f"{ctx.step_number:03d}__{ctx.current_step_name}"
        step_dir.mkdir(parents=True, exist_ok=True)
        collected_files: list[str] = []
        warnings: list[str] = []

        ns_args = ["-n", self.namespace] if self.namespace else []

        # Collect pod logs
        if self.collect_pod_logs:
            log_file = step_dir / "app_logs.txt"
            result = self._run_oc(
                ["logs", "-l", f"app={self.app_label}", "--tail=1000", *ns_args],
                log_file,
            )
            if result:
                collected_files.append(str(log_file))
            else:
                warnings.append(f"Failed to collect logs for app={self.app_label}")

        # Collect pod descriptions
        if self.collect_pod_describe:
            describe_file = step_dir / "pod_describe.txt"
            result = self._run_oc(
                ["describe", "pods", "-l", f"app={self.app_label}", *ns_args],
                describe_file,
            )
            if result:
                collected_files.append(str(describe_file))
            else:
                warnings.append(f"Failed to describe pods for app={self.app_label}")

        # Collect events
        if self.collect_events:
            events_file = step_dir / "events.txt"
            result = self._run_oc(
                ["get", "events", "--sort-by=.lastTimestamp", *ns_args],
                events_file,
            )
            if result:
                collected_files.append(str(events_file))
            else:
                warnings.append("Failed to collect events")

        # Collect pod status
        status_file = step_dir / "pod_status.txt"
        result = self._run_oc(
            ["get", "pods", "-l", f"app={self.app_label}", "-o", "wide", *ns_args],
            status_file,
        )
        if result:
            collected_files.append(str(status_file))

        message = f"Collected {len(collected_files)} artifacts"
        if warnings:
            message += f" ({len(warnings)} warnings)"
            for w in warnings:
                logger.warning(w)

        return StepResult(
            success=True,  # Never fail - this is a finally step
            message=message,
            artifacts=collected_files,
        )

    def _run_oc(self, args: list[str], output_file: Path) -> bool:
        """
        Run oc command and write output to file.

        Returns True if successful, False otherwise.
        """
        try:
            cmd = ["oc", *args]
            logger.debug(f"Running: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Write output regardless of exit code
            with open(output_file, "w") as f:
                f.write(f"# Command: oc {' '.join(args)}\n")
                f.write(f"# Exit code: {result.returncode}\n\n")
                if result.stdout:
                    f.write(result.stdout)
                if result.stderr:
                    f.write(f"\n# STDERR:\n{result.stderr}")

            return result.returncode == 0

        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out: oc {' '.join(args)}")
            return False
        except FileNotFoundError:
            logger.warning("oc command not found")
            return False
        except Exception as e:
            logger.warning(f"Error running oc: {e}")
            return False


class CleanupDeploymentStep(WorkflowStep):
    """
    Clean up Kubernetes/KServe deployment resources.

    Runs as a finally step to ensure resources are cleaned up
    even on failure. Handles both standard K8s deployments and
    KServe InferenceService/ServingRuntime resources.
    """

    def __init__(
        self,
        deployment_name: str,
        namespace: str | None = None,
        delete_service: bool = True,
        delete_route: bool = True,
        use_kserve: bool = True,
        name: str | None = None,
    ):
        """
        Initialize cleanup step.

        Args:
            deployment_name: Name of the deployment/InferenceService to delete
            namespace: Kubernetes namespace (uses current context if None)
            delete_service: Also delete the associated service
            delete_route: Also delete the associated route
            use_kserve: Delete KServe resources (InferenceService, ServingRuntime)
            name: Optional step name
        """
        super().__init__(name=name or "cleanup")
        self.deployment_name = deployment_name
        self.namespace = namespace
        self.delete_service = delete_service
        self.delete_route = delete_route
        self.use_kserve = use_kserve

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Delete deployment and related resources."""
        ns_args = ["-n", self.namespace] if self.namespace else []
        deleted: list[str] = []
        errors: list[str] = []

        # Delete KServe resources first (they manage the underlying deployments)
        if self.use_kserve:
            if self._delete_resource("inferenceservice", self.deployment_name, ns_args):
                deleted.append(f"inferenceservice/{self.deployment_name}")
            if self._delete_resource("servingruntime", self.deployment_name, ns_args):
                deleted.append(f"servingruntime/{self.deployment_name}")

        # Delete standard deployment (if not using KServe or as fallback)
        if self._delete_resource("deployment", self.deployment_name, ns_args):
            deleted.append(f"deployment/{self.deployment_name}")

        # Delete service
        if self.delete_service:
            if self._delete_resource("service", self.deployment_name, ns_args):
                deleted.append(f"service/{self.deployment_name}")

        # Delete route
        if self.delete_route:
            if self._delete_resource("route", self.deployment_name, ns_args):
                deleted.append(f"route/{self.deployment_name}")

        message = f"Deleted: {', '.join(deleted)}" if deleted else "Nothing deleted"
        if errors:
            message += f" (errors: {len(errors)})"

        return StepResult(
            success=True,  # Never fail - this is a finally step
            message=message,
            data={"deleted": deleted, "errors": errors},
        )

    def _delete_resource(self, kind: str, name: str, ns_args: list[str]) -> bool:
        """Delete a Kubernetes resource. Returns True if successful."""
        try:
            cmd = ["oc", "delete", kind, name, "--ignore-not-found", *ns_args]
            logger.debug(f"Running: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.returncode == 0

        except Exception as e:
            logger.warning(f"Error deleting {kind}/{name}: {e}")
            return False
