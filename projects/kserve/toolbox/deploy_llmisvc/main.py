#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path

import yaml

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils import write_text
from projects.core.dsl.utils.k8s import (
    oc,
    oc_apply,
    oc_get_json,
)


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@entrypoint
def run(
    *,
    namespace: str,
    inference_service_manifest_path: str,
    gateway_status_address_name: str = "gateway-external",
) -> str:
    """
    Deploy an LLMInferenceService and wait for its endpoint.

    Args:
        namespace: Namespace used by llm_d
        inference_service_manifest_path: Path to the InferenceService YAML manifest file
        gateway_status_address_name: Gateway status address name for endpoint resolution
    """

    # Load manifest to extract the service name
    manifest = load_yaml(Path(inference_service_manifest_path))
    inference_service_name = manifest["metadata"]["name"]

    # Pass only the required arguments to avoid including manifest content in logs
    task_args = {
        "namespace": namespace,
        "inference_service_manifest_path": inference_service_manifest_path,
        "inference_service_name": inference_service_name,
        "gateway_status_address_name": gateway_status_address_name,
    }
    context = execute_tasks(task_args)

    # Ensure endpoint_url is available
    endpoint_url = getattr(context, "endpoint_url", None)
    if not endpoint_url:
        raise RuntimeError("Failed to resolve gateway endpoint URL after deployment")

    # Log service description for visibility
    service_description = getattr(context, "service_description", None)
    if service_description:
        print(f"Deployed: {service_description}")

    return endpoint_url


@task
def copy_manifest_to_src(args, ctx):
    """Copy inference service manifest to src directory for inspection and use"""
    import shutil

    # Get the original manifest path
    original_path = Path(args.inference_service_manifest_path)

    # Ensure the src directory exists
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Copy the manifest to src directory
    src_path = src_dir / original_path.name
    shutil.copy2(original_path, src_path)

    # Store the src path in context for other tasks to use
    ctx.src_manifest_path = str(src_path)

    return f"Copied manifest from {original_path} to {src_path}"


@task
def capture_llm_service_description(args, ctx):
    """Capture LLM service description using oc describe"""

    # Use oc describe to get the deployed service description
    result = oc(
        "describe",
        "llminferenceservice",
        ctx.service_name,
        "-n",
        args.namespace,
        log_stdout=False,
    )

    service_description = result.stdout.strip()

    # Store in context for other tasks
    ctx.service_description = service_description

    return f"Captured LLM service description for {ctx.service_name}"


@task
def delete_existing_service(args, ctx):
    """Delete existing LLMInferenceService"""

    name = args.inference_service_name
    oc(
        "delete",
        "llminferenceservice",
        name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        check=False,
    )
    ctx.service_name = name
    ctx.selector = f"app.kubernetes.io/name={name}"
    return f"Deleted existing LLMInferenceService {name}"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_old_pods_gone(args, ctx):
    """Wait for old llm-d pods to disappear"""

    pods = oc_get_json(
        "pods", namespace=args.namespace, selector=ctx.selector, ignore_not_found=True
    )
    if not pods or not pods.get("items"):
        return f"Old pods gone for {ctx.service_name}"
    return False  # Retry


@task
def apply_inference_service(args, ctx):
    """Apply the LLMInferenceService manifest"""

    # Use the manifest copied to src directory
    src_manifest_path = ctx.src_manifest_path

    # Load and apply the manifest from src
    manifest = load_yaml(Path(src_manifest_path))
    oc_apply(src_manifest_path, manifest)
    return f"Applied LLMInferenceService manifest from {src_manifest_path} for {ctx.service_name}"


@retry(attempts=120, delay=5, backoff=1.0)
@task
def wait_pods_appear(args, ctx):
    """Wait for llm-d pods to appear"""

    pods = oc_get_json(
        "pods", namespace=args.namespace, selector=ctx.selector, ignore_not_found=True
    )
    if pods and pods.get("items"):
        return f"Pods appeared for {ctx.service_name}"
    return False  # Retry


@task
def query_service_status(args, ctx):
    """Query the status of the LLMInferenceService"""

    # Query only the Ready condition status
    result = oc(
        "get",
        "llminferenceservice",
        ctx.service_name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
        log_stdout=False,
    )

    ready_status = result.stdout.strip()
    ctx.is_ready = ready_status == "True"

    if ctx.is_ready:
        return f"LLMInferenceService {ctx.service_name} status: Ready"
    else:
        return f"LLMInferenceService {ctx.service_name} status: Not Ready"


@task
def query_service_message(args, ctx):
    """Query detailed message from LLMInferenceService"""

    # Query the Ready condition details
    result = oc(
        "get",
        "llminferenceservice",
        ctx.service_name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.status.conditions[?(@.type=='Ready')]}",
        log_stdout=False,
    )

    if result.stdout.strip():
        try:
            import json

            condition = json.loads(result.stdout)
            reason = condition.get("reason", "Unknown")
            message = condition.get("message", "No message")

            if not ctx.is_ready:
                return f"Not ready - Reason: {reason}, Message: {message}"
            else:
                return "Ready - Service is operational"
        except (json.JSONDecodeError, KeyError) as e:
            return f"Failed to parse Ready condition: {e}"
    else:
        return "No Ready condition found in status"


@retry(attempts=90, delay=10, backoff=1.0)
@task
def wait_service_ready(args, ctx):
    """Wait for LLMInferenceService to be ready"""

    # Query the current status and show diagnostic info
    result = oc(
        "get",
        "llminferenceservice",
        ctx.service_name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.status.conditions[?(@.type=='Ready')]}",
        log_stdout=True,
    )

    # Also show pod status for debugging
    oc(
        "get",
        "pods",
        "-l",
        ctx.selector,
        "-n",
        args.namespace,
        log_stdout=True,  # Show pod status in logs
    )

    if result.stdout.strip():
        try:
            import json

            condition = json.loads(result.stdout)
            status = condition.get("status", "Unknown")
            reason = condition.get("reason", "Unknown")
            message = condition.get("message", "No message")

            if status == "True":
                return f"LLMInferenceService {ctx.service_name} is ready"
            else:
                return (
                    False,
                    f"Service not ready - Status: {status}, Reason: {reason}, Message: {message}",
                )

        except (json.JSONDecodeError, KeyError) as e:
            return (False, f"Failed to parse Ready condition: {e}")
    else:
        return (False, f"No Ready condition found in status for {ctx.service_name}")


@retry(attempts=90, delay=10, backoff=1.0)
@task
def resolve_endpoint_task(args, ctx):
    """Resolve the gateway endpoint URL"""

    endpoint_url = try_resolve_endpoint_url(
        namespace=args.namespace,
        inference_service_name=args.inference_service_name,
        gateway_status_address_name=args.gateway_status_address_name,
    )
    if endpoint_url:
        ctx.endpoint_url = endpoint_url
        write_text(args.artifact_dir / "artifacts" / "endpoint.url", f"{endpoint_url}\n")
        return f"Endpoint resolved: {endpoint_url}"
    return False  # Retry


@task
def deploy_llmisvc_task(args, ctx):
    """Deploy the llm_d inference service and resolve its endpoint"""

    # All work is done by the individual tasks
    service_info = getattr(
        ctx, "service_description", f"LLM Service '{args.inference_service_name}'"
    )
    return (
        f"LLMInferenceService deployment completed: {service_info} - Endpoint: {ctx.endpoint_url}"
    )


def try_resolve_endpoint_url(
    *, namespace: str, inference_service_name: str, gateway_status_address_name: str
) -> str | None:
    payload = oc_get_json("llminferenceservice", name=inference_service_name, namespace=namespace)

    for address in payload.get("status", {}).get("addresses", []):
        if address.get("name") == gateway_status_address_name and address.get("url"):
            return address["url"]
    return None


if __name__ == "__main__":
    run.main()
