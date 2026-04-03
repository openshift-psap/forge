"""GuideLLM benchmark step - runs as a pod on the cluster."""

import json
import logging
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

from projects.core.workflow import StepResult, WorkflowStep

if TYPE_CHECKING:
    from projects.core.workflow import WorkflowContext

logger = logging.getLogger(__name__)

# Default GuideLLM image from llm-d-bench
DEFAULT_GUIDELLM_IMAGE = "ghcr.io/openshift-psap/llm-d-bench/guidellm:latest"


class RunGuideLLMStep(WorkflowStep):
    """
    Run GuideLLM benchmark as a pod on the cluster.

    Deploys a GuideLLM pod in the same namespace as the inference service,
    waits for completion, and collects results.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        namespace: str = "forge",
        workload: str = "balanced",
        max_requests: int | None = None,
        max_seconds: int = 120,
        rate: str = "1,50,100",
        rate_type: str = "concurrent",
        guidellm_image: str | None = None,
        output_file: str = "guidellm_results.json",
        name: str | None = None,
    ):
        """
        Initialize GuideLLM step.

        Args:
            endpoint: Inference endpoint URL (e.g., http://vllm-svc:8080/v1)
            model: Model name as deployed
            namespace: Kubernetes namespace where to run the benchmark pod
            workload: GuideLLM workload type (balanced, heterogeneous, multiturn)
                      or explicit format: "prompt_tokens=1000,output_tokens=1000"
            max_requests: Maximum number of requests to send
            max_seconds: Maximum benchmark duration in seconds per rate
            rate: Comma-separated rates to test (e.g., "1,50,100")
            rate_type: Rate type - "concurrent" or "synchronous"
            guidellm_image: GuideLLM container image
            output_file: Name of output file in artifact directory
            name: Optional step name
        """
        super().__init__(name=name or "benchmark")
        self.endpoint = endpoint
        self.model = model
        self.namespace = namespace
        self.workload = workload
        self.max_requests = max_requests
        self.max_seconds = max_seconds
        self.rate = rate
        self.rate_type = rate_type
        self.guidellm_image = guidellm_image or DEFAULT_GUIDELLM_IMAGE
        self.output_file = output_file
        # Use model name in pod name for easier correlation with inference pods
        model_short = model.split("/")[-1].lower().replace(".", "-").replace("_", "-")[:20]
        self.pod_name = f"guidellm-{model_short}-{uuid.uuid4().hex[:6]}"

    def execute(self, ctx: "WorkflowContext") -> StepResult:
        """Run GuideLLM benchmark as a pod."""
        step_dir = ctx.artifact_dir / f"{ctx.step_number:03d}__{ctx.current_step_name}"
        step_dir.mkdir(parents=True, exist_ok=True)

        # Convert workload to GuideLLM data format
        data = self._workload_to_data(self.workload)

        # Generate pod YAML
        pod_yaml = self._generate_pod_yaml(data)
        yaml_path = step_dir / "guidellm-pod.yaml"
        yaml_path.write_text(pod_yaml)

        logger.info(f"Creating GuideLLM pod: {self.pod_name}")
        print(f"Creating GuideLLM pod: {self.pod_name} in namespace {self.namespace}")

        # Create the pod
        try:
            result = subprocess.run(
                ["oc", "apply", "-f", str(yaml_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return StepResult.fail(
                    f"Failed to create GuideLLM pod: {result.stderr}",
                    error=RuntimeError(result.stderr),
                )
        except Exception as e:
            return StepResult.fail(f"Failed to create pod: {e}", error=e)

        # Wait for pod to complete
        # Calculate generous timeout: max_seconds per rate, plus 30min overhead for startup/warmup
        num_rates = len(self.rate.split(","))
        timeout = (self.max_seconds * num_rates) + 1800  # 30min overhead
        wait_result = self._wait_for_pod_completion(timeout)

        # Collect logs regardless of outcome
        self._collect_pod_logs(step_dir)

        # Cleanup pod
        self._delete_pod()

        if not wait_result["success"]:
            return StepResult.fail(
                f"GuideLLM pod failed: {wait_result['message']}",
                error=RuntimeError(wait_result["message"]),
            )

        return StepResult.ok(
            f"GuideLLM completed in {wait_result.get('duration', 0):.1f}s",
            pod_name=self.pod_name,
        )

    def _workload_to_data(self, workload: str) -> str:
        """Convert workload name to GuideLLM data format."""
        workload_map = {
            "balanced": "prompt_tokens=1000,output_tokens=1000",
            "short": "prompt_tokens=256,output_tokens=256",
            "long-prompt": "prompt_tokens=8000,output_tokens=1000",
            "very-long-prompt": "prompt_tokens=16000,output_tokens=1000",
            "heterogeneous": "emulated",
            "multi-turn": "multi_turn",
        }
        return workload_map.get(workload, workload)

    def _generate_pod_yaml(self, data: str) -> str:
        """Generate GuideLLM pod YAML."""
        # Build guidellm args
        args = [
            "--target", self.endpoint,
            "--model", self.model,
            "--rate", self.rate,
            "--rate-type", self.rate_type,
            "--data", data,
            "--max-seconds", str(self.max_seconds),
            "--backend-type", "openai_http",
        ]

        if self.max_requests:
            args.extend(["--max-requests", str(self.max_requests)])

        # Build command as shell script: run benchmark, signal completion, sleep for rsync
        guidellm_cmd = f"python3 -m benchmark.main {' '.join(args)}"

        return f"""apiVersion: v1
kind: Pod
metadata:
  name: {self.pod_name}
  namespace: {self.namespace}
  labels:
    app: guidellm-benchmark
    forge-run: "true"
spec:
  restartPolicy: Never
  containers:
  - name: guidellm
    image: {self.guidellm_image}
    imagePullPolicy: Always
    command:
    - /bin/sh
    - -c
    - |
      {guidellm_cmd}
      echo "BENCHMARK_COMPLETE"
      echo "Sleeping 30s for artifact collection..."
      sleep 30
    env:
    - name: HF_TOKEN
      valueFrom:
        secretKeyRef:
          name: storage-config
          key: HF_TOKEN
          optional: true
    - name: GUIDELLM__REQUEST_TIMEOUT
      value: "6000"
    - name: GUIDELLM__LOGGING__CONSOLE_LOG_LEVEL
      value: "INFO"
    - name: HF_HOME
      value: /tmp/.huggingface
    resources:
      requests:
        cpu: "1"
        memory: "2Gi"
      limits:
        cpu: "4"
        memory: "8Gi"
    volumeMounts:
    - name: results-volume
      mountPath: /benchmark-results
  volumes:
  - name: results-volume
    emptyDir: {{}}
  # Avoid GPU nodes - run on infra/worker nodes
  affinity:
    nodeAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
          - key: nvidia.com/gpu
            operator: DoesNotExist
"""

    def _wait_for_pod_completion(self, timeout: int) -> dict:
        """Wait for benchmark to complete (watching for BENCHMARK_COMPLETE marker in logs).

        The pod runs benchmark, prints BENCHMARK_COMPLETE, then sleeps for 30s.
        We detect completion via the marker while pod is still running,
        allowing rsync to work before the pod exits.
        """
        start_time = time.monotonic()
        poll_interval = 10

        print(f"Waiting for GuideLLM benchmark to complete (timeout: {timeout}s)...")

        while time.monotonic() - start_time < timeout:
            try:
                # First check pod phase
                phase_result = subprocess.run(
                    [
                        "oc", "get", "pod", self.pod_name,
                        "-n", self.namespace,
                        "-o", "jsonpath={.status.phase}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                phase = phase_result.stdout.strip()

                if phase == "Failed":
                    return {"success": False, "message": "Pod failed"}

                if phase == "Error":
                    return {"success": False, "message": "Pod error"}

                # Check logs for BENCHMARK_COMPLETE marker
                if phase in ("Running", "Succeeded"):
                    log_result = subprocess.run(
                        ["oc", "logs", self.pod_name, "-n", self.namespace, "--tail=50"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if "BENCHMARK_COMPLETE" in log_result.stdout:
                        duration = time.monotonic() - start_time
                        print(f"GuideLLM benchmark completed in {duration:.1f}s")
                        return {"success": True, "duration": duration}

                # Also handle case where pod already Succeeded (marker might have been missed)
                if phase == "Succeeded":
                    duration = time.monotonic() - start_time
                    print(f"GuideLLM pod completed in {duration:.1f}s")
                    return {"success": True, "duration": duration}

                # Still running, no marker yet
                elapsed = int(time.monotonic() - start_time)
                if elapsed % 60 == 0:  # Print every minute
                    print(f"  GuideLLM running... ({elapsed}s elapsed, phase={phase})")

            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                logger.warning(f"Error checking pod status: {e}")

            time.sleep(poll_interval)

        return {"success": False, "message": f"Timeout after {timeout}s"}

    def _collect_pod_logs(self, step_dir):
        """Collect logs and results from the GuideLLM pod."""
        # Collect logs
        try:
            result = subprocess.run(
                ["oc", "logs", self.pod_name, "-n", self.namespace],
                capture_output=True,
                text=True,
                timeout=60,
            )
            (step_dir / "guidellm_logs.txt").write_text(result.stdout)
            if result.stderr:
                (step_dir / "guidellm_stderr.txt").write_text(result.stderr)

            print(f"GuideLLM logs saved to {step_dir}/guidellm_logs.txt")

        except Exception as e:
            logger.warning(f"Failed to collect pod logs: {e}")

        # Copy results from pod before it's deleted (use rsync for large files)
        try:
            results_dir = step_dir / "results"
            results_dir.mkdir(exist_ok=True)

            # Use oc rsync for efficient transfer of large files (up to 300MB)
            result = subprocess.run(
                [
                    "oc", "rsync",
                    f"{self.pod_name}:/benchmark-results/",
                    str(results_dir),
                    "-n", self.namespace,
                    "--progress",
                ],
                capture_output=True,
                text=True,
                timeout=600,  # 10 min timeout for large files
            )
            if result.returncode == 0:
                print(f"GuideLLM results synced to {results_dir}/")
            else:
                logger.warning(f"Failed to rsync results: {result.stderr}")

        except subprocess.TimeoutExpired:
            logger.warning("Timeout copying results (>10 min)")
        except Exception as e:
            logger.warning(f"Failed to copy results from pod: {e}")

    def _delete_pod(self):
        """Delete the GuideLLM pod."""
        try:
            subprocess.run(
                ["oc", "delete", "pod", self.pod_name, "-n", self.namespace, "--ignore-not-found"],
                capture_output=True,
                timeout=30,
            )
            print(f"Cleaned up GuideLLM pod: {self.pod_name}")
        except Exception as e:
            logger.warning(f"Failed to delete pod: {e}")
