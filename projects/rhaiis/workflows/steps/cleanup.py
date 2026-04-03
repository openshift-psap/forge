"""Cleanup steps for RHAIIS."""

import logging
import subprocess
from typing import TYPE_CHECKING

from projects.core.workflow import StepResult, WorkflowStep

if TYPE_CHECKING:
    from projects.core.workflow import WorkflowContext

logger = logging.getLogger(__name__)


class CleanupNamespaceStep(WorkflowStep):
    """Clean up all resources in a namespace."""

    def __init__(
        self,
        namespace: str,
        delete_namespace: bool = False,
        name: str | None = None,
    ):
        """
        Initialize cleanup step.

        Args:
            namespace: Namespace to clean up
            delete_namespace: Whether to delete the namespace itself
            name: Optional step name
        """
        super().__init__(name=name or "cleanup_namespace")
        self.namespace = namespace
        self.delete_namespace = delete_namespace

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Delete all resources in namespace."""
        deleted_resources: list[str] = []
        errors: list[str] = []

        # Resource types to delete (KServe resources first, then standard K8s)
        resource_types = [
            "inferenceservice",
            "servingruntime",
            "deployment",
            "service",
            "route",
            "configmap",
            "secret",
            "pod",
        ]

        for resource_type in resource_types:
            try:
                result = subprocess.run(
                    [
                        "oc", "delete", resource_type,
                        "--all",
                        "-n", self.namespace,
                        "--ignore-not-found",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                if result.returncode == 0:
                    deleted_resources.append(resource_type)
                else:
                    errors.append(f"{resource_type}: {result.stderr}")

            except subprocess.TimeoutExpired:
                errors.append(f"{resource_type}: timeout")
            except Exception as e:
                errors.append(f"{resource_type}: {e}")

        # Optionally delete namespace
        if self.delete_namespace:
            try:
                result = subprocess.run(
                    ["oc", "delete", "namespace", self.namespace, "--ignore-not-found"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0:
                    deleted_resources.append(f"namespace/{self.namespace}")
            except Exception as e:
                errors.append(f"namespace: {e}")

        message = f"Cleaned up {len(deleted_resources)} resource types"
        if errors:
            message += f" ({len(errors)} errors)"
            for err in errors:
                logger.warning(f"Cleanup error: {err}")

        return StepResult(
            success=True,  # Don't fail on cleanup errors
            message=message,
            data={"deleted": deleted_resources, "errors": errors},
        )
