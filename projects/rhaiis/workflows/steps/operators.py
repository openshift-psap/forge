"""Operator installation steps for RHAIIS."""

import logging
import subprocess
from typing import TYPE_CHECKING

from projects.core.workflow import StepResult, WorkflowStep

if TYPE_CHECKING:
    from projects.core.workflow import WorkflowContext

logger = logging.getLogger(__name__)


class InstallNFDOperatorStep(WorkflowStep):
    """Install Node Feature Discovery operator."""

    def __init__(self, name: str | None = None):
        super().__init__(name=name or "install_nfd")

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Install NFD operator."""
        step_dir = ctx.artifact_dir / f"{ctx.step_number:03d}__{ctx.current_step_name}"

        subscription_yaml = """
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: nfd
  namespace: openshift-nfd
spec:
  channel: stable
  name: nfd
  source: redhat-operators
  sourceNamespace: openshift-marketplace
"""
        yaml_path = step_dir / "nfd-subscription.yaml"
        yaml_path.write_text(subscription_yaml)

        # Create namespace first
        self._run_oc(["create", "namespace", "openshift-nfd", "--dry-run=client", "-o", "yaml"])
        self._run_oc(["apply", "-f", str(yaml_path)])

        return StepResult.ok("NFD operator subscription created")

    def _run_oc(self, args: list[str]) -> bool:
        """Run oc command."""
        try:
            result = subprocess.run(
                ["oc", *args],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"oc command failed: {e}")
            return False


class InstallGPUOperatorStep(WorkflowStep):
    """Install NVIDIA GPU operator."""

    def __init__(self, name: str | None = None):
        super().__init__(name=name or "install_gpu")

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Install GPU operator."""
        step_dir = ctx.artifact_dir / f"{ctx.step_number:03d}__{ctx.current_step_name}"

        subscription_yaml = """
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: gpu-operator-certified
  namespace: nvidia-gpu-operator
spec:
  channel: v24.6
  name: gpu-operator-certified
  source: certified-operators
  sourceNamespace: openshift-marketplace
"""
        yaml_path = step_dir / "gpu-subscription.yaml"
        yaml_path.write_text(subscription_yaml)

        # Create namespace first
        subprocess.run(
            ["oc", "create", "namespace", "nvidia-gpu-operator"],
            capture_output=True,
        )

        result = subprocess.run(
            ["oc", "apply", "-f", str(yaml_path)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return StepResult.fail(f"Failed to install GPU operator: {result.stderr}")

        return StepResult.ok("GPU operator subscription created")


class InstallRHOAIOperatorStep(WorkflowStep):
    """Install Red Hat OpenShift AI operator."""

    def __init__(self, version: str = "2.19", name: str | None = None):
        super().__init__(name=name or "install_rhoai")
        self.version = version

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Install RHOAI operator."""
        step_dir = ctx.artifact_dir / f"{ctx.step_number:03d}__{ctx.current_step_name}"

        # Determine channel from version
        channel = f"stable-{self.version}"

        subscription_yaml = f"""
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: rhods-operator
  namespace: redhat-ods-operator
spec:
  channel: {channel}
  name: rhods-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
"""
        yaml_path = step_dir / "rhoai-subscription.yaml"
        yaml_path.write_text(subscription_yaml)

        # Create namespace first
        subprocess.run(
            ["oc", "create", "namespace", "redhat-ods-operator"],
            capture_output=True,
        )

        result = subprocess.run(
            ["oc", "apply", "-f", str(yaml_path)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return StepResult.fail(f"Failed to install RHOAI operator: {result.stderr}")

        return StepResult.ok(f"RHOAI operator {self.version} subscription created")
