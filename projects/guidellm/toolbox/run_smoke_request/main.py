#!/usr/bin/env python3

from __future__ import annotations

import json
import logging

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils import write_json, write_text
from projects.core.dsl.utils.k8s import (
    oc,
    oc_apply,
    oc_get_json,
    oc_resource_exists,
)
from projects.guidellm.toolbox.run_smoke_request.utils import render_smoke_request_pod_from_parts

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    namespace: str,
    endpoint_url: str,
    pod_name: str = "llm-d-smoke",
    client_image: str = "quay.io/curl/curl:8.21.0",
    endpoint_path: str = "/v1/completions",
    request_timeout_seconds: int = 60,
    served_model_name: str,
    prompt: str = "San Francisco is a",
    max_tokens: int = 50,
    temperature: float = 0.7,
    api_key_file_path: str | None = None,
) -> dict[str, object]:
    """
    Run the llm_d smoke request against a resolved endpoint using a pod.

    Args:
        namespace: Namespace used by llm_d
        endpoint_url: Gateway endpoint URL returned by the deploy command
        pod_name: Name for the smoke test pod
        client_image: Container image for making HTTP requests
        endpoint_path: API endpoint path to test
        request_timeout_seconds: Timeout for the request
        served_model_name: Model name to use in API requests
        prompt: Test prompt to send
        max_tokens: Maximum tokens to generate
        temperature: Sampling temperature
        api_key_file_path: Path to a local file containing the API key/token.
            When set, a K8s Secret is created, mounted into the pod, and
            passed as a Bearer token in the curl request.
    """

    context = execute_tasks(locals())
    return context.response


@task
def validate_endpoint(args, ctx):
    """Validate that endpoint_url is provided and not None"""

    if not args.endpoint_url:
        raise ValueError("endpoint_url cannot be None or empty")

    return f"Validated endpoint URL: {args.endpoint_url}"


@task
def prepare_smoke_request(args, ctx):
    """Prepare smoke request payload"""

    ctx.pod_name = args.pod_name

    payload = {
        "model": args.served_model_name,
        "prompt": args.prompt,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }
    ctx.payload = payload

    # Ensure artifacts directory exists
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    write_json(artifacts_dir / "smoke.request.json", payload)
    return f"Prepared smoke request for pod {args.pod_name}"


_API_KEY_MOUNT_PATH = "/secrets/auth-token/auth_token"


@task
def create_api_key_secret(args, ctx):
    """Create a K8s Secret from the API key file (if provided)."""
    if not args.api_key_file_path:
        ctx.api_key_secret_name = ""
        return "No API key file provided"

    from pathlib import Path

    token_file = Path(args.api_key_file_path)
    if not token_file.exists():
        raise FileNotFoundError(f"API key file does not exist: {args.api_key_file_path}")

    secret_name = f"{args.pod_name}-auth-token"

    oc(
        "delete",
        "secret",
        secret_name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        check=False,
    )

    oc(
        "create",
        "secret",
        "generic",
        secret_name,
        f"--from-file=auth_token={args.api_key_file_path}",
        "-n",
        args.namespace,
    )

    ctx.api_key_secret_name = secret_name
    return f"API key secret {secret_name} created from file"


@task
def delete_existing_smoke_pod(args, ctx):
    """Delete existing smoke pod"""

    oc(
        "delete",
        "pod",
        ctx.pod_name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        check=False,
    )
    return f"Initiated deletion of pod {ctx.pod_name}"


@retry(attempts=24, delay=5, backoff=1.0)
@task
def wait_smoke_pod_deleted(args, ctx):
    """Wait for smoke pod deletion to complete"""

    if not oc_resource_exists("pod", ctx.pod_name, namespace=args.namespace):
        return f"Pod {ctx.pod_name} deleted"
    return False  # Retry


@task
def create_smoke_pod(args, ctx):
    """Create the smoke request pod"""

    # Ensure the src directory exists
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    api_key_secret = getattr(ctx, "api_key_secret_name", "") or ""
    oc_apply(
        src_dir / "smoke-pod.yaml",
        render_smoke_request_pod_from_parts(
            namespace=args.namespace,
            pod_name=args.pod_name,
            client_image=args.client_image,
            api_key_secret=api_key_secret,
        ),
    )
    return f"Created smoke pod {ctx.pod_name}"


@task
def set_secret_owner_ref(args, ctx):
    """Set ownerReference on the API key secret to the smoke pod for garbage collection."""
    if not ctx.api_key_secret_name:
        return "No API key secret, skipping owner ref"

    pod_uid = oc(
        "get",
        "pod",
        ctx.pod_name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.metadata.uid}",
        log_stdout=True,
    ).stdout.strip()

    patch = json.dumps(
        {
            "metadata": {
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": ctx.pod_name,
                        "uid": pod_uid,
                    }
                ]
            }
        }
    )
    oc(
        "patch",
        "secret",
        ctx.api_key_secret_name,
        "-n",
        args.namespace,
        "--type=merge",
        f"-p={patch}",
    )

    return f"Set owner ref on {ctx.api_key_secret_name} → pod {ctx.pod_name}"


@retry(attempts=12, delay=5, backoff=1.0)
@task
def wait_smoke_pod_running(args, ctx):
    """Wait for smoke pod to be running"""

    # Check pod status
    payload = oc_get_json(
        "pod",
        name=ctx.pod_name,
        namespace=args.namespace,
        ignore_not_found=True,
    )
    if not payload:
        return (False, f"Pod {ctx.pod_name} not found, retrying...")

    phase = payload.get("status", {}).get("phase")
    if phase == "Running":
        return f"Smoke pod {ctx.pod_name} is running"
    elif phase in ["Failed", "Succeeded"]:
        raise RuntimeError(f"Pod {ctx.pod_name} entered unexpected phase: {phase}")

    # Still pending
    return (False, f"Smoke pod {ctx.pod_name} still in phase: {phase or 'Unknown'}, retrying...")


@task
def execute_smoke_request(args, ctx):
    """Execute the curl command inside the running pod"""

    api_key_secret = getattr(ctx, "api_key_secret_name", "") or ""

    if api_key_secret:
        import shlex

        payload_json = shlex.quote(json.dumps(ctx.payload))
        url = shlex.quote(f"{args.endpoint_url}{args.endpoint_path}")
        script = (
            f"curl -k -sS --fail-with-body --max-time {args.request_timeout_seconds}"
            f" {url}"
            f" -H 'Content-Type: application/json'"
            f' -H "Authorization: Bearer $(cat {_API_KEY_MOUNT_PATH})"'
            f" -d {payload_json}"
        )
        exec_cmd = ["sh", "-c", script]
    else:
        exec_cmd = [
            "curl",
            "-k",
            "-sS",
            "--fail-with-body",
            "--max-time",
            str(args.request_timeout_seconds),
            f"{args.endpoint_url}{args.endpoint_path}",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(ctx.payload),
        ]

    # Execute the command in the pod (single attempt)
    result = oc(
        "exec",
        ctx.pod_name,
        "-n",
        args.namespace,
        "-c",
        "smoke",
        "--",
        *exec_cmd,
        check=False,
    )

    # Save stdout and stderr for debugging
    if result.stdout:
        write_text(args.artifact_dir / "artifacts" / "smoke.curl.stdout.txt", result.stdout)
    if result.stderr:
        write_text(args.artifact_dir / "artifacts" / "smoke.curl.stderr.txt", result.stderr)

    if result.returncode != 0 or not result.stdout:
        # Request failed
        raise RuntimeError(
            f"Smoke request failed (exit {result.returncode}): {result.stderr or 'No error message'}"
        )

    response = None
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON response: {result.stdout}") from e

    if not response.get("choices"):
        raise ValueError(f"Invalid response format: {result.stdout}")

    ctx.response = response
    write_json(args.artifact_dir / "artifacts" / "smoke.response.json", response)

    # Log the response for visibility
    logger.info("Smoke request response: %s", json.dumps(response, indent=2))

    # Log summary of the response
    choices = response.get("choices", [])
    if choices:
        first_choice_text = choices[0].get("text", "").strip()
        logger.info(
            "Generated text: %s",
            first_choice_text[:200] + "..." if len(first_choice_text) > 200 else first_choice_text,
        )

    return "Smoke request completed successfully"


@always
@task
def capture_smoke_pod_debug_info(args, ctx):
    """Capture pod YAML and description for debugging (always runs)"""

    if not hasattr(ctx, "pod_name"):
        return "No pod name available for debug capture"

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    try:
        oc(
            "get",
            "pod",
            ctx.pod_name,
            "-n",
            args.namespace,
            "-o",
            "yaml",
            check=False,
            stdout_dest=artifacts_dir / f"{ctx.pod_name}.yaml",
        )

        oc(
            "describe",
            "pod",
            ctx.pod_name,
            "-n",
            args.namespace,
            check=False,
            stdout_dest=artifacts_dir / f"{ctx.pod_name}.describe.txt",
        )

        return f"Captured debug info for pod {ctx.pod_name}"

    except Exception as e:
        # Don't fail the task if debug capture fails
        logger.warning(f"Failed to capture debug info for pod {ctx.pod_name}: {e}")
        return f"Failed to capture debug info: {e}"


@always
@task
def cleanup_smoke_pod(args, ctx):
    """Delete the smoke pod after test completion"""

    if not hasattr(ctx, "pod_name"):
        return "No pod name available for cleanup"

    oc(
        "delete",
        "pod",
        ctx.pod_name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        "--wait=false",
        check=False,
    )
    return f"Cleaned up smoke pod {ctx.pod_name}"


if __name__ == "__main__":
    run.main()
