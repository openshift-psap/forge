"""RHAIIS vLLM deployment steps using KServe (ServingRuntime + InferenceService)."""

import logging
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Any

from projects.core.workflow import StepResult, WorkflowStep

if TYPE_CHECKING:
    from projects.core.workflow import WorkflowContext

logger = logging.getLogger(__name__)


class DeployVLLMStep(WorkflowStep):
    """
    Deploy vLLM serving on OpenShift using KServe.

    Creates a ServingRuntime and InferenceService for vLLM model serving.
    This is the recommended deployment method for RHAIIS/RHOAI.
    """

    def __init__(
        self,
        model: str,
        deployment_name: str,
        vllm_image: str,
        runtime_args: dict[str, Any],
        namespace: str = "forge",
        tensor_parallel: int | None = None,
        replicas: int = 1,
        accelerator: str = "nvidia",
        storage_source: str = "hf",
        storage_path: str | None = None,
        cpu_request: str = "4",
        memory_request: str = "16Gi",
        env_vars: dict[str, str] | None = None,
        name: str | None = None,
    ):
        """
        Initialize vLLM deployment step.

        Args:
            model: HuggingFace model ID (e.g., Qwen/Qwen3-0.6B)
            deployment_name: Name for K8s resources (ServingRuntime, InferenceService)
            vllm_image: Container image for vLLM (from config)
            runtime_args: vLLM runtime arguments (from config, includes all vllm_args)
            namespace: Kubernetes namespace
            tensor_parallel: Override tensor parallelism (default: from runtime_args)
            replicas: Number of replicas (minReplicas)
            accelerator: GPU accelerator type ("nvidia" or "amd")
            storage_source: Model storage source ("hf" for HuggingFace, "s3", "pvc")
            storage_path: Storage path (PVC name for hf, bucket path for s3)
            cpu_request: CPU request
            memory_request: Memory request
            env_vars: Environment variables (from config)
            name: Optional step name
        """
        super().__init__(name=name or "deploy")
        self.model = model
        self.deployment_name = deployment_name
        self.accelerator = accelerator.lower()
        self.vllm_image = vllm_image
        self.namespace = namespace
        self.replicas = replicas
        self.cpu_request = cpu_request
        self.memory_request = memory_request
        self.storage_source = storage_source
        self.storage_path = storage_path

        # Use runtime_args directly from config
        self.runtime_args = dict(runtime_args)

        # tensor_parallel: use explicit override or get from runtime_args
        self.tensor_parallel = tensor_parallel or self.runtime_args.get("tensor-parallel-size", 1)

        self.env_vars = env_vars or {}
        self.deployment_uuid = str(uuid.uuid4())[:8]

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Deploy vLLM to OpenShift using KServe."""
        step_dir = ctx.artifact_dir / f"{ctx.step_number:03d}__{ctx.current_step_name}"
        step_dir.mkdir(parents=True, exist_ok=True)

        # Generate KServe YAML (ServingRuntime + InferenceService)
        kserve_yaml = self._generate_kserve_yaml()
        yaml_path = step_dir / "kserve.yaml"
        yaml_path.write_text(kserve_yaml)

        # Ensure namespace exists
        subprocess.run(
            ["oc", "create", "namespace", self.namespace, "--dry-run=client", "-o", "yaml"],
            capture_output=True,
        )
        subprocess.run(
            ["oc", "apply", "-f", "-"],
            input=f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {self.namespace}\n",
            capture_output=True,
            text=True,
        )

        # Apply KServe resources
        try:
            result = subprocess.run(
                ["oc", "apply", "-f", str(yaml_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                return StepResult.fail(
                    f"Failed to apply KServe resources: {result.stderr}",
                    error=RuntimeError(result.stderr),
                )

            logger.info(f"Deployed InferenceService {self.deployment_name} to {self.namespace}")
            return StepResult.ok(
                f"Deployed {self.deployment_name}",
                deployment_name=self.deployment_name,
                namespace=self.namespace,
                deployment_uuid=self.deployment_uuid,
            )

        except subprocess.TimeoutExpired as e:
            return StepResult.fail("Deployment timed out", error=e)
        except Exception as e:
            return StepResult.fail(f"Deployment error: {e}", error=e)

    def _generate_kserve_yaml(self) -> str:
        """Generate KServe ServingRuntime and InferenceService YAML."""
        # Build vLLM args
        args_lines = self._build_args_lines()

        # Build env vars
        env_lines = self._build_env_lines()

        # Shared memory volume (always needed for vLLM)
        volume_mounts = """
    volumeMounts:
    - name: shared-memory
      mountPath: /dev/shm"""
        volumes = """
  volumes:
  - name: shared-memory
    emptyDir:
      medium: Memory
      sizeLimit: 8Gi"""

        # GPU resource type based on accelerator
        gpu_resource = "nvidia.com/gpu" if self.accelerator == "nvidia" else "amd.com/gpu"

        # Storage URI based on source
        storage_uri = self._build_storage_uri()

        return f"""---
apiVersion: serving.kserve.io/v1alpha1
kind: ServingRuntime
metadata:
  annotations:
    opendatahub.io/template-display-name: ServingRuntime for vLLM | Forge
  labels:
    opendatahub.io/dashboard: "true"
  name: {self.deployment_name}
  namespace: {self.namespace}
spec:
  builtInAdapter:
    modelLoadingTimeoutMillis: 300000
  imagePullSecrets:
  - name: npalaska-image-pull
  containers:
  - command:
    - python3
    - -m
    - vllm.entrypoints.openai.api_server
    args:
{args_lines}
    env:
{env_lines}
    image: "{self.vllm_image}"
    name: kserve-container
    ports:
    - containerPort: 8080
      protocol: TCP{volume_mounts}
  multiModel: false
  supportedModelFormats:
  - autoSelect: true
    name: pytorch{volumes}
---
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/path: "/metrics"
    prometheus.io/port: "8000"
    serving.kserve.io/deploymentMode: RawDeployment
    serving.kserve.io/enable-prometheus-scraping: "true"
    storage.kserve.io/readonly: "false"
  labels:
    opendatahub.io/dashboard: "true"
    deployment_uuid: {self.deployment_uuid}
    app: {self.deployment_name}
  name: {self.deployment_name}
  namespace: {self.namespace}
spec:
  predictor:
    minReplicas: {self.replicas}
    model:
      resources:
        limits:
          {gpu_resource}: "{self.tensor_parallel}"
        requests:
          {gpu_resource}: "{self.tensor_parallel}"
          cpu: "{self.cpu_request}"
          memory: "{self.memory_request}"
      runtime: {self.deployment_name}
      modelFormat:
        name: pytorch
      storageUri: {storage_uri}
    serviceAccountName: sa
"""

    def _build_args_lines(self) -> str:
        """Build vLLM command line arguments."""
        lines = []

        # Model argument depends on storage source
        if self.storage_source == "hf":
            lines.append(f"    - --model={self.model}")
        else:
            lines.append("    - --model=/mnt/models")
            lines.append(f"    - --served-model-name={self.model}")

        lines.append("    - --port=8080")

        # Add runtime args
        for key, val in self.runtime_args.items():
            if isinstance(val, bool):
                if val:
                    lines.append(f"    - --{key}")
            else:
                lines.append(f"    - --{key}={val}")

        return "\n".join(lines)

    def _build_env_lines(self) -> str:
        """Build environment variables."""
        lines = []


        # HuggingFace storage source env vars
        if self.storage_source == "hf":
            lines.extend([
                "    - name: HF_HUB_OFFLINE",
                '      value: "0"',
                "    - name: HOME",
                "      value: /mnt/models",
                "    - name: HF_HOME",
                "      value: /mnt/models",
                "    - name: VLLM_CACHE_DIR",
                "      value: /mnt/models/.cache/vllm",
                "    - name: HF_DATASETS_CACHE",
                "      value: /mnt/models/.cache/huggingface/datasets",
                "    - name: HF_TOKEN",
                "      valueFrom:",
                "        secretKeyRef:",
                "          name: storage-config",
                "          key: HF_TOKEN",
            ])

        # Additional env vars
        for key, val in self.env_vars.items():
            lines.append(f"    - name: {key}")
            lines.append(f'      value: "{val}"')

        return "\n".join(lines) if lines else "    []"

    def _build_storage_uri(self) -> str:
        """Build storage URI for InferenceService."""
        if self.storage_source == "hf":
            # Use PVC for HuggingFace models (model-pvc-2 is the default on H200)
            pvc_name = self.storage_path or "model-pvc-2"
            return f"pvc://{pvc_name}"
        elif self.storage_path:
            return f"{self.storage_source}://{self.storage_path}"
        else:
            return f"{self.storage_source}://{self.model}"


class WaitForReadyStep(WorkflowStep):
    """Wait for InferenceService to become ready."""

    def __init__(
        self,
        deployment_name: str,
        namespace: str = "forge",
        timeout_seconds: int = 3600,
        poll_interval: int = 10,
        name: str | None = None,
    ):
        """
        Initialize wait step.

        Args:
            deployment_name: Name of InferenceService to wait for
            namespace: Kubernetes namespace
            timeout_seconds: Maximum wait time
            poll_interval: Seconds between status checks
            name: Optional step name
        """
        super().__init__(name=name or "wait")
        self.deployment_name = deployment_name
        self.namespace = namespace
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Wait for InferenceService to be ready."""
        import click

        click.echo(
            f"Waiting for InferenceService {self.deployment_name} to be ready "
            f"(timeout: {self.timeout_seconds}s)..."
        )

        start_time = time.monotonic()
        last_status_print = 0

        while time.monotonic() - start_time < self.timeout_seconds:
            elapsed = int(time.monotonic() - start_time)

            # Print status every 30 seconds
            if elapsed - last_status_print >= 30:
                click.echo(f"  Still waiting... ({elapsed}s elapsed)")
                last_status_print = elapsed
            try:
                # Check InferenceService status
                result = subprocess.run(
                    [
                        "oc", "get", "inferenceservice",
                        self.deployment_name,
                        "-n", self.namespace,
                        "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

                if result.returncode == 0 and result.stdout.strip() == "True":
                    elapsed = time.monotonic() - start_time
                    logger.info(f"InferenceService ready in {elapsed:.1f}s")

                    # Get the service URL
                    url_result = subprocess.run(
                        [
                            "oc", "get", "inferenceservice",
                            self.deployment_name,
                            "-n", self.namespace,
                            "-o", "jsonpath={.status.url}",
                        ],
                        capture_output=True,
                        text=True,
                    )

                    # Health check: verify vLLM endpoint is actually responding
                    endpoint = f"http://{self.deployment_name}-predictor.{self.namespace}.svc.cluster.local:8080"
                    health_ok = self._wait_for_health_check(endpoint)
                    if not health_ok:
                        return StepResult.fail(
                            f"InferenceService ready but health check failed after {self.timeout_seconds}s"
                        )

                    total_elapsed = time.monotonic() - start_time
                    return StepResult.ok(
                        f"InferenceService ready and healthy in {total_elapsed:.1f}s",
                        ready_time_seconds=elapsed,
                        health_check_time_seconds=total_elapsed - elapsed,
                        service_url=url_result.stdout.strip() if url_result.returncode == 0 else None,
                    )

                # Also check underlying deployment for debugging
                deploy_result = subprocess.run(
                    [
                        "oc", "rollout", "status",
                        f"deployment/{self.deployment_name}-predictor",
                        "-n", self.namespace,
                        "--timeout=5s",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if deploy_result.returncode == 0:
                    logger.debug("Underlying deployment is ready, waiting for InferenceService...")

            except subprocess.TimeoutExpired:
                pass  # Continue waiting
            except Exception as e:
                logger.warning(f"Error checking status: {e}")

            time.sleep(self.poll_interval)

        # Timeout - collect debug info
        self._log_debug_info()

        return StepResult.fail(
            f"InferenceService not ready after {self.timeout_seconds}s"
        )

    def _log_debug_info(self):
        """Log debug information on timeout."""
        try:
            # Get InferenceService status
            result = subprocess.run(
                ["oc", "get", "inferenceservice", self.deployment_name, "-n", self.namespace, "-o", "yaml"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.error(f"InferenceService status:\n{result.stdout}")

            # Get pod status
            result = subprocess.run(
                ["oc", "get", "pods", "-l", f"serving.kserve.io/inferenceservice={self.deployment_name}",
                 "-n", self.namespace],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.error(f"Pod status:\n{result.stdout}")
        except Exception as e:
            logger.warning(f"Failed to collect debug info: {e}")

    def _wait_for_health_check(self, endpoint: str, timeout: int = 120, interval: int = 5) -> bool:
        """
        Wait for vLLM health endpoint to respond.

        Uses oc exec to curl the health endpoint from within the cluster.

        Args:
            endpoint: vLLM service endpoint URL
            timeout: Maximum wait time in seconds
            interval: Seconds between health check attempts

        Returns:
            True if health check passes, False on timeout
        """
        import click

        click.echo(f"Verifying vLLM health check at {endpoint}/health ...")

        start_time = time.monotonic()

        while time.monotonic() - start_time < timeout:
            try:
                # Get a pod name to exec into for health check
                pod_result = subprocess.run(
                    [
                        "oc", "get", "pods",
                        "-l", f"serving.kserve.io/inferenceservice={self.deployment_name}",
                        "-n", self.namespace,
                        "-o", "jsonpath={.items[0].metadata.name}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if pod_result.returncode != 0 or not pod_result.stdout.strip():
                    logger.warning("No pod found for health check")
                    time.sleep(interval)
                    continue

                pod_name = pod_result.stdout.strip()

                # Try /health endpoint via localhost (pod-internal)
                health_result = subprocess.run(
                    [
                        "oc", "exec", pod_name,
                        "-n", self.namespace,
                        "-c", "kserve-container",
                        "--",
                        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        "http://localhost:8080/health",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

                elapsed = int(time.monotonic() - start_time)

                if health_result.returncode == 0 and health_result.stdout.strip() == "200":
                    click.echo(f"  Health check passed ({elapsed}s)")
                    return True
                else:
                    click.echo(f"  Health check pending... ({elapsed}s, status={health_result.stdout.strip()})")

            except subprocess.TimeoutExpired:
                click.echo(f"  Health check timeout, retrying...")
            except Exception as e:
                logger.warning(f"Health check error: {e}")

            time.sleep(interval)

        click.echo(f"  Health check failed after {timeout}s")
        return False
