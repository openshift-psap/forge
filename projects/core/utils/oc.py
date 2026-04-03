"""OpenShift CLI wrapper with built-in retry for transient failures.

Provides a clean, method-based API for oc commands with automatic retry
on transient network errors, API server unavailability, etc.

Example:
    from projects.core.utils import OC, RetryConfig

    # Basic usage
    oc = OC(namespace="forge")
    result = oc.get("pods")
    if result.success:
        print(result.stdout)

    # With custom retry config
    oc = OC(namespace="forge", retry=RetryConfig(max_retries=5))
    result = oc.apply("-f", "manifest.yaml")

    # Without namespace (uses current context)
    oc = OC()
    result = oc.get("namespaces")
"""

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Error patterns that indicate transient failures worth retrying
TRANSIENT_ERROR_PATTERNS = [
    "connection refused",
    "connection reset",
    "connection timed out",
    "unable to connect",
    "no route to host",
    "temporary failure",
    "service unavailable",
    "server is currently unable",
    "etcdserver: request timed out",
    "context deadline exceeded",
    "the server was unable to return a response",
    "unexpected eof",
    "i/o timeout",
    "tls handshake timeout",
    "net/http: request canceled",
    "client rate limiter",
    "too many requests",
    "throttling",
    "apiserver not ready",
]


@dataclass
class RetryConfig:
    """Configuration for retry behavior.

    Attributes:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay between retries in seconds (default: 1.0)
        max_delay: Maximum delay between retries in seconds (default: 30.0)
        backoff_multiplier: Multiplier for exponential backoff (default: 2.0)
        retry_on_timeout: Whether to retry on subprocess timeout (default: True)
    """

    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    backoff_multiplier: float = 2.0
    retry_on_timeout: bool = True


@dataclass
class OCResult:
    """Result of an oc command execution.

    Attributes:
        success: Whether the command succeeded (returncode == 0)
        returncode: Command exit code
        stdout: Standard output as string
        stderr: Standard error as string
        command: The command that was executed
        attempts: Number of attempts made (1 = no retries needed)
        duration: Total execution time including retries in seconds
    """

    success: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)
    attempts: int = 1
    duration: float = 0.0

    @classmethod
    def from_completed_process(
        cls,
        result: subprocess.CompletedProcess,
        command: list[str],
        attempts: int = 1,
        duration: float = 0.0,
    ) -> "OCResult":
        """Create OCResult from subprocess.CompletedProcess."""
        return cls(
            success=result.returncode == 0,
            returncode=result.returncode,
            stdout=result.stdout if result.stdout else "",
            stderr=result.stderr if result.stderr else "",
            command=command,
            attempts=attempts,
            duration=duration,
        )

    @classmethod
    def from_error(
        cls,
        error: Exception,
        command: list[str],
        attempts: int = 1,
        duration: float = 0.0,
    ) -> "OCResult":
        """Create failed OCResult from exception."""
        return cls(
            success=False,
            returncode=-1,
            stdout="",
            stderr=str(error),
            command=command,
            attempts=attempts,
            duration=duration,
        )


def _is_transient_error(stderr: str, returncode: int) -> bool:
    """Check if error is likely transient and worth retrying."""
    if returncode == 0:
        return False
    stderr_lower = stderr.lower()
    return any(pattern in stderr_lower for pattern in TRANSIENT_ERROR_PATTERNS)


class OC:
    """OpenShift CLI wrapper with built-in retry.

    Provides a clean, method-based API for common oc operations.
    All methods automatically retry on transient failures.

    Args:
        namespace: Default namespace for commands (optional)
        retry: Retry configuration (uses defaults if None)
        timeout: Default command timeout in seconds (default: 60)

    Example:
        oc = OC(namespace="forge")

        # Get pods
        result = oc.get("pods")
        result = oc.get("pods", "-l", "app=vllm")
        result = oc.get("pod", "my-pod", "-o", "yaml")

        # Apply manifests
        result = oc.apply("-f", "manifest.yaml")
        result = oc.apply("-f", "-", input=yaml_content)

        # Delete resources
        result = oc.delete("pod", "my-pod")
        result = oc.delete("pod", "my-pod", "--ignore-not-found")

        # Logs
        result = oc.logs("my-pod")
        result = oc.logs("my-pod", "-c", "container", "--tail=100")

        # Exec
        result = oc.exec("my-pod", "--", "curl", "localhost:8080/health")

        # Raw command
        result = oc.run("get", "pods", "-A")
    """

    def __init__(
        self,
        namespace: str | None = None,
        retry: RetryConfig | None = None,
        timeout: int = 60,
    ):
        self.namespace = namespace
        self.retry = retry or RetryConfig()
        self.timeout = timeout

    def _build_cmd(self, args: list[str], namespace: str | None = None) -> list[str]:
        """Build full oc command with namespace."""
        cmd = ["oc"]

        # Use provided namespace, fall back to instance default
        ns = namespace if namespace is not None else self.namespace
        if ns:
            cmd.extend(["-n", ns])

        cmd.extend(args)
        return cmd

    def _run_with_retry(
        self,
        cmd: list[str],
        timeout: int | None = None,
        input: str | None = None,
    ) -> OCResult:
        """Execute command with retry on transient failures."""
        timeout = timeout if timeout is not None else self.timeout
        delay = self.retry.initial_delay
        attempts = 0
        start_time = time.monotonic()

        last_result: subprocess.CompletedProcess | None = None
        last_error: Exception | None = None

        for attempt in range(self.retry.max_retries + 1):
            attempts = attempt + 1
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    input=input,
                )

                # Success
                if result.returncode == 0:
                    duration = time.monotonic() - start_time
                    if attempts > 1:
                        logger.info(
                            f"Command succeeded on attempt {attempts}: {' '.join(cmd[:4])}"
                        )
                    return OCResult.from_completed_process(
                        result, cmd, attempts, duration
                    )

                # Check if transient error
                if _is_transient_error(result.stderr, result.returncode):
                    last_result = result
                    if attempt < self.retry.max_retries:
                        logger.warning(
                            f"Transient error (attempt {attempts}/{self.retry.max_retries + 1}), "
                            f"retrying in {delay:.1f}s: {' '.join(cmd[:4])}..."
                        )
                        time.sleep(delay)
                        delay = min(delay * self.retry.backoff_multiplier, self.retry.max_delay)
                        continue

                # Non-transient error or exhausted retries
                duration = time.monotonic() - start_time
                return OCResult.from_completed_process(result, cmd, attempts, duration)

            except subprocess.TimeoutExpired as e:
                last_error = e
                if self.retry.retry_on_timeout and attempt < self.retry.max_retries:
                    logger.warning(
                        f"Timeout (attempt {attempts}/{self.retry.max_retries + 1}), "
                        f"retrying in {delay:.1f}s: {' '.join(cmd[:4])}..."
                    )
                    time.sleep(delay)
                    delay = min(delay * self.retry.backoff_multiplier, self.retry.max_delay)
                    continue

                duration = time.monotonic() - start_time
                return OCResult.from_error(e, cmd, attempts, duration)

            except FileNotFoundError as e:
                # oc command not found - don't retry
                duration = time.monotonic() - start_time
                return OCResult.from_error(
                    Exception("oc command not found. Is OpenShift CLI installed?"),
                    cmd,
                    attempts,
                    duration,
                )

            except Exception as e:
                # Unexpected error - don't retry
                duration = time.monotonic() - start_time
                return OCResult.from_error(e, cmd, attempts, duration)

        # Exhausted retries
        duration = time.monotonic() - start_time
        if last_error:
            return OCResult.from_error(last_error, cmd, attempts, duration)
        if last_result:
            return OCResult.from_completed_process(last_result, cmd, attempts, duration)

        return OCResult.from_error(
            Exception("Unexpected state after retries"),
            cmd,
            attempts,
            duration,
        )

    def run(self, *args: str, namespace: str | None = None, timeout: int | None = None, input: str | None = None) -> OCResult:
        """Run arbitrary oc command.

        Args:
            *args: Command arguments (e.g., "get", "pods", "-o", "yaml")
            namespace: Override namespace for this command
            timeout: Override timeout for this command
            input: Input to pass to stdin

        Returns:
            OCResult with command output
        """
        cmd = self._build_cmd(list(args), namespace)
        return self._run_with_retry(cmd, timeout, input)

    def get(self, resource: str, *args: str, namespace: str | None = None, timeout: int | None = None) -> OCResult:
        """Get Kubernetes resources.

        Args:
            resource: Resource type (e.g., "pods", "deployments")
            *args: Additional arguments (name, selectors, output format)
            namespace: Override namespace
            timeout: Override timeout

        Returns:
            OCResult with resource data

        Examples:
            oc.get("pods")
            oc.get("pods", "-l", "app=vllm")
            oc.get("pod", "my-pod", "-o", "yaml")
            oc.get("pods", "-o", "jsonpath={.items[*].metadata.name}")
        """
        return self.run("get", resource, *args, namespace=namespace, timeout=timeout)

    def apply(self, *args: str, namespace: str | None = None, timeout: int | None = None, input: str | None = None) -> OCResult:
        """Apply configuration to resources.

        Args:
            *args: Apply arguments (e.g., "-f", "manifest.yaml")
            namespace: Override namespace
            timeout: Override timeout
            input: YAML content to apply via stdin (use with "-f", "-")

        Returns:
            OCResult

        Examples:
            oc.apply("-f", "manifest.yaml")
            oc.apply("-f", "-", input=yaml_content)
        """
        return self.run("apply", *args, namespace=namespace, timeout=timeout, input=input)

    def delete(self, resource: str, name: str = "", *args: str, namespace: str | None = None, timeout: int | None = None) -> OCResult:
        """Delete resources.

        Args:
            resource: Resource type
            name: Resource name (optional for label selectors)
            *args: Additional arguments (--ignore-not-found, etc.)
            namespace: Override namespace
            timeout: Override timeout

        Returns:
            OCResult

        Examples:
            oc.delete("pod", "my-pod")
            oc.delete("pod", "my-pod", "--ignore-not-found")
            oc.delete("pods", "-l", "app=test")
        """
        if name:
            return self.run("delete", resource, name, *args, namespace=namespace, timeout=timeout)
        return self.run("delete", resource, *args, namespace=namespace, timeout=timeout)

    def logs(self, pod: str, *args: str, namespace: str | None = None, timeout: int | None = None) -> OCResult:
        """Get pod logs.

        Args:
            pod: Pod name
            *args: Additional arguments (-c container, --tail, --since, etc.)
            namespace: Override namespace
            timeout: Override timeout

        Returns:
            OCResult with logs in stdout

        Examples:
            oc.logs("my-pod")
            oc.logs("my-pod", "-c", "sidecar")
            oc.logs("my-pod", "--tail=100")
        """
        return self.run("logs", pod, *args, namespace=namespace, timeout=timeout)

    def exec(self, pod: str, *args: str, namespace: str | None = None, timeout: int | None = None) -> OCResult:
        """Execute command in pod.

        Args:
            pod: Pod name
            *args: Command to execute (use "--" separator)
            namespace: Override namespace
            timeout: Override timeout

        Returns:
            OCResult with command output

        Examples:
            oc.exec("my-pod", "--", "curl", "localhost:8080/health")
            oc.exec("my-pod", "-c", "container", "--", "cat", "/etc/config")
        """
        return self.run("exec", pod, *args, namespace=namespace, timeout=timeout)

    def describe(self, resource: str, name: str = "", *args: str, namespace: str | None = None, timeout: int | None = None) -> OCResult:
        """Describe resources.

        Args:
            resource: Resource type
            name: Resource name (optional)
            *args: Additional arguments
            namespace: Override namespace
            timeout: Override timeout

        Returns:
            OCResult with description
        """
        if name:
            return self.run("describe", resource, name, *args, namespace=namespace, timeout=timeout)
        return self.run("describe", resource, *args, namespace=namespace, timeout=timeout)

    def wait(
        self,
        resource: str,
        name: str,
        condition: str,
        timeout_seconds: int = 300,
        namespace: str | None = None,
    ) -> OCResult:
        """Wait for resource condition.

        Args:
            resource: Resource type
            name: Resource name
            condition: Condition to wait for (e.g., "condition=Ready")
            timeout_seconds: Wait timeout in seconds
            namespace: Override namespace

        Returns:
            OCResult

        Example:
            oc.wait("pod", "my-pod", "condition=Ready", timeout_seconds=120)
        """
        return self.run(
            "wait",
            f"{resource}/{name}",
            f"--for={condition}",
            f"--timeout={timeout_seconds}s",
            namespace=namespace,
            timeout=timeout_seconds + 10,  # Give subprocess a bit more time
        )

    def rollout_status(
        self,
        resource: str,
        name: str,
        timeout_seconds: int = 300,
        namespace: str | None = None,
    ) -> OCResult:
        """Check rollout status.

        Args:
            resource: Resource type (deployment, statefulset, etc.)
            name: Resource name
            timeout_seconds: Timeout for rollout
            namespace: Override namespace

        Returns:
            OCResult
        """
        return self.run(
            "rollout",
            "status",
            f"{resource}/{name}",
            f"--timeout={timeout_seconds}s",
            namespace=namespace,
            timeout=timeout_seconds + 10,
        )

    def rsync(
        self,
        source: str,
        dest: str,
        *args: str,
        namespace: str | None = None,
        timeout: int | None = None,
    ) -> OCResult:
        """Rsync files to/from pod.

        Args:
            source: Source path (pod:path or local path)
            dest: Destination path
            *args: Additional rsync arguments
            namespace: Override namespace
            timeout: Override timeout

        Returns:
            OCResult

        Example:
            oc.rsync("my-pod:/data/", "./local/")
            oc.rsync("./local/", "my-pod:/data/", "--progress")
        """
        return self.run("rsync", source, dest, *args, namespace=namespace, timeout=timeout)

    def create_namespace(self, name: str) -> OCResult:
        """Create namespace if it doesn't exist.

        Args:
            name: Namespace name

        Returns:
            OCResult
        """
        # Use apply with dry-run to create idempotently
        yaml_content = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {name}
"""
        return self.apply("-f", "-", input=yaml_content, namespace=None)
