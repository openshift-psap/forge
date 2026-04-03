"""RHAIIS cleanup workflow - remove deployments and optionally operators."""

from projects.core.workflow import Workflow, WorkflowContext
from projects.rhaiis.workflows.steps import CleanupNamespaceStep


class CleanupWorkflow(Workflow):
    """
    Cleanup RHAIIS resources.

    Removes deployments from the benchmark namespace.
    Optionally can remove operators (not enabled by default).
    """

    def __init__(
        self,
        ctx: WorkflowContext,
        namespace: str = "forge",
        remove_operators: bool = False,
    ):
        """
        Initialize cleanup workflow.

        Args:
            ctx: Workflow context
            namespace: Namespace to clean up
            remove_operators: Whether to also remove operators
        """
        super().__init__(ctx)
        self.namespace = namespace
        self.remove_operators = remove_operators

    def define_steps(self):
        """Define cleanup steps."""
        self.add_step(
            CleanupNamespaceStep(
                namespace=self.namespace,
                delete_namespace=False,  # Keep namespace, delete contents
            )
        )
