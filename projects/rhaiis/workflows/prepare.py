"""RHAIIS prepare workflow - install operators."""

from projects.core.workflow import Workflow, WorkflowContext
from projects.rhaiis.workflows.steps import (
    InstallGPUOperatorStep,
    InstallNFDOperatorStep,
    InstallRHOAIOperatorStep,
)


class PrepareWorkflow(Workflow):
    """
    Prepare cluster for RHAIIS benchmarking.

    Installs required operators:
    1. NFD (Node Feature Discovery) Operator
    2. GPU Operator (NVIDIA or AMD)
    3. RHOAI (Red Hat OpenShift AI) Operator
    """

    def __init__(self, ctx: WorkflowContext, rhoai_version: str = "2.19"):
        """
        Initialize prepare workflow.

        Args:
            ctx: Workflow context
            rhoai_version: RHOAI operator version
        """
        super().__init__(ctx)
        self.rhoai_version = rhoai_version

    def define_steps(self):
        """Define operator installation steps."""
        self.add_step(InstallNFDOperatorStep())
        self.add_step(InstallGPUOperatorStep())
        self.add_step(InstallRHOAIOperatorStep(version=self.rhoai_version))
