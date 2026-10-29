"""
Kubernetes utilities for DSL tasks
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl import shell

logger = logging.getLogger(__name__)


def oc(
    *args: str,
    check: bool = True,
    input_text: str | None = None,
    timeout_seconds: float | None = 300,
    log_stdout: bool = True,
    log_stderr: bool = True,
    stdout_dest: Path | None = None,
    text: bool = True,
    handled_secretly: bool = False,
) -> shell.CommandResult:
    """Run an oc command.

    Args:
        *args: Arguments to pass to oc
        check: Raise subprocess.CalledProcessError if command fails
        input_text: Input to send to command
        timeout_seconds: Command timeout
        log_stdout: Log stdout to console (default True)
        log_stderr: Log stderr to console (default True)
        stdout_dest: Optional file path to write stdout to
        text: Optional. If False, handle binary output (default True)
        handled_secretly: If True, disable all logging to prevent secret leakage

    Returns:
        CommandResult with execution details
    """
    cmd = ["oc", *args]

    # When handled secretly, disable all logging to prevent credential leakage
    if handled_secretly:
        log_stdout = False
        log_stderr = False

    try:
        return shell.run(
            cmd,
            check=check,
            shell=False,
            log_stdout=log_stdout,
            log_stderr=log_stderr,
            stdout_dest=stdout_dest,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            text=text,
        )
    except subprocess.CalledProcessError as e:
        if not handled_secretly:
            raise

        # When handled secretly, sanitize error to prevent credential leakage
        raise subprocess.CalledProcessError(
            e.returncode,
            "oc command failed",
            "",
            "Error output suppressed for secret handling",
        ) from e


def oc_get_json(
    kind: str,
    *,
    name: str | None = None,
    namespace: str | None = None,
    selector: str | None = None,
    ignore_not_found: bool = False,
    handled_secretly: bool = False,
) -> dict[str, Any] | None:
    """Get a Kubernetes resource as JSON using oc.

    Args:
        kind: Resource kind (e.g., 'pod', 'deployment')
        name: Resource name (optional)
        namespace: Namespace (optional)
        selector: Label selector (optional)
        ignore_not_found: Return None instead of raising error if not found
        handled_secretly: If True, disable all logging to prevent secret leakage

    Returns:
        Resource data as dict, or None if not found and ignore_not_found=True

    Raises:
        subprocess.CalledProcessError: If oc command fails
    """
    args = ["get", kind]
    if name:
        args.append(name)
    if namespace:
        args.extend(["-n", namespace])
    if selector:
        args.extend(["-l", selector])
    args.extend(["-o", "json"])

    # When handled secretly, disable all logging to prevent credential leakage
    log_stderr_enabled = not ignore_not_found and not handled_secretly

    result = oc(
        *args,
        check=not ignore_not_found,
        log_stdout=False,  # Always disabled to prevent data exposure
        log_stderr=log_stderr_enabled,  # Disabled when handled secretly
        handled_secretly=handled_secretly,
    )
    if result.returncode != 0:
        if ignore_not_found and _is_oc_not_found_error(result.stderr):
            return None
        # Re-raise error (oc() already sanitized if handled_secretly=True)
        raise subprocess.CalledProcessError(
            result.returncode, f"oc {' '.join(args)}", result.stdout, result.stderr
        )
    if not result.stdout:
        error_msg = (
            f"oc get {kind} returned no output"
            if handled_secretly
            else f"oc {' '.join(args)} returned no output"
        )
        raise ValueError(error_msg)
    return json.loads(result.stdout)


def oc_resource_exists(kind: str, name: str, *, namespace: str | None = None) -> bool:
    """Check if a Kubernetes resource exists.

    Args:
        kind: Resource kind (e.g., 'pod', 'deployment')
        name: Resource name
        namespace: Namespace (optional)

    Returns:
        True if resource exists, False otherwise
    """
    args = ["get", kind, name]
    if namespace is not None:
        args.extend(["-n", namespace])
    args.extend(["--ignore-not-found", "-oname"])

    exists = oc(*args, check=False)

    return bool(exists.stdout)


def _is_oc_not_found_error(stderr: str | None) -> bool:
    """Check if stderr contains a 'not found' error from oc."""
    if not stderr:
        return False

    normalized = stderr.lower()
    if "error from server (notfound)" in normalized:
        return True
    if "no resources found" in normalized:
        return True

    return bool(re.search(r"\bnot found\b", normalized))


def oc_apply(artifact_path: Any, manifest: dict[str, Any]) -> None:
    """Apply a Kubernetes manifest.

    Args:
        artifact_path: Path where to write the manifest YAML (for record keeping)
        manifest: Manifest data as dict
    """
    # Write the manifest to the file
    with open(artifact_path, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False)

    oc("apply", "-f", str(artifact_path))


def condition_status(resource: dict[str, Any], condition_type: str) -> str | None:
    """Get the status of a specific condition from a Kubernetes resource.

    Args:
        resource: Resource data as dict
        condition_type: Type of condition to look for

    Returns:
        Condition status (e.g., 'True', 'False', 'Unknown') or None if not found
    """
    for condition in resource.get("status", {}).get("conditions", []):
        if condition.get("type") == condition_type:
            return condition.get("status")
    return None


def sanitize_k8s_name(name: str) -> str:
    """
    Sanitize a name to be compatible with Kubernetes object naming requirements.

    Kubernetes object names must:
    - Be lowercase only
    - Only contain alphanumeric characters and hyphens
    - Start and end with alphanumeric characters
    - Be maximum 63 characters long

    Args:
        name: The name to sanitize

    Returns:
        A valid Kubernetes object name

    Examples:
        >>> sanitize_k8s_name("My_Test Job!")
        "my-test-job-x"
        >>> sanitize_k8s_name("forge-llm_d-20260409-143022")
        "forge-llm-d-20260409-143022"
        >>> sanitize_k8s_name("valid-name123")
        "valid-name123"
    """
    # Convert to lowercase and replace invalid characters with hyphens
    sanitized = re.sub(r"[^a-z0-9\-]", "-", name.lower())

    # Remove leading/trailing hyphens and collapse multiple hyphens
    sanitized = re.sub(r"^-+|-+$", "", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized)

    # Ensure it starts and ends with alphanumeric
    if sanitized and not sanitized[0].isalnum():
        sanitized = "x" + sanitized
    if sanitized and not sanitized[-1].isalnum():
        sanitized = sanitized + "x"

    # Truncate to 63 characters (K8s limit)
    if len(sanitized) > 63:
        sanitized = sanitized[:63]
        # Make sure it still ends with alphanumeric after truncation
        if not sanitized[-1].isalnum():
            sanitized = sanitized[:-1] + "x"

    return sanitized or "default"


def is_valid_k8s_name(name: str) -> bool:
    """
    Check if a name is valid for Kubernetes objects.

    Args:
        name: The name to validate

    Returns:
        True if the name is valid, False otherwise

    Examples:
        >>> is_valid_k8s_name("valid-name123")
        True
        >>> is_valid_k8s_name("Invalid_Name")
        False
        >>> is_valid_k8s_name("toolongname" * 10)
        False
    """
    if not name:
        return False

    # Check length
    if len(name) > 63:
        return False

    # Check pattern: lowercase alphanumeric and hyphens, start/end with alphanumeric
    pattern = r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$"
    return bool(re.match(pattern, name))


def capture_pod_logs(*, namespace: str, output_dir: Path) -> None:
    """Capture logs from all pods in a namespace to individual files.

    Args:
        namespace: Kubernetes namespace to collect from
        output_dir: Directory to write pod log files into
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = oc(
        "get",
        "pods",
        "-n",
        namespace,
        "-o",
        'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        logger.warning("Could not list pods in namespace %s", namespace)
        return

    pod_names = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
    logger.info("Capturing logs from %d pods in %s", len(pod_names), namespace)

    for pod_name in pod_names:
        log_file = output_dir / f"{pod_name}.log"
        log_result = oc(
            "logs",
            pod_name,
            "-n",
            namespace,
            "--all-containers=true",
            check=False,
            log_stdout=False,
            stdout_dest=log_file,
        )
        # If command failed and no output was written, write error info
        if log_result.returncode != 0 and (not log_file.exists() or log_file.stat().st_size == 0):
            log_file.write_text(
                f"(failed to collect logs: exit_code={log_result.returncode})\n"
                f"{log_result.stderr or ''}",
                encoding="utf-8",
            )


def oc_exec(
    *command: str,
    namespace: str,
    pod: str,
    container: str | None = None,
    **kwargs,
) -> shell.CommandResult:
    """Exec a command inside a pod container.

    Args:
        *command: Command and arguments to run in the pod
        namespace: Namespace of the pod
        pod: Pod name
        container: Container name (optional if pod has a single container)
        **kwargs: Additional keyword arguments passed to oc()

    Returns:
        CommandResult with execution details
    """
    args = ["-n", namespace, "exec", pod]
    if container:
        args.extend(["-c", container])
    args.append("--")
    args.extend(command)
    return oc(*args, **kwargs)


def oc_cp_from_pod(
    remote_path: str,
    local_path: str | Path,
    *,
    namespace: str,
    pod: str,
    container: str | None = None,
) -> shell.CommandResult:
    """Copy a file from a pod to a local path.

    Args:
        remote_path: Absolute path inside the pod
        local_path: Local destination path
        namespace: Namespace of the pod
        pod: Pod name
        container: Container name (optional if pod has a single container)

    Returns:
        CommandResult with execution details
    """
    args = ["cp", "--retries=99", f"{namespace}/{pod}:{remote_path}", str(local_path)]
    if container:
        args.extend(["-c", container])
    return oc(*args)


def best_effort_oc(*oc_args: str, description: str | None = None) -> None:
    """Run an oc command, swallowing timeout and other errors.

    Useful in cleanup paths where partial failure should not abort the
    remaining cleanup steps.
    """
    label = description or f"oc {' '.join(oc_args)}"
    try:
        oc(*oc_args, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out: %s", label)
    except Exception as exc:
        logger.warning("Error: %s: %s", label, exc)
