#!/usr/bin/env python3

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from projects.core.dsl import (
    EarlyReturn,
    always,
    entrypoint,
    execute_tasks,
    on_failure,
    retry,
    task,
)
from projects.core.dsl.template import render_template
from projects.core.dsl.utils import slugify_identifier, write_text
from projects.core.dsl.utils.k8s import (
    oc,
    oc_apply,
    oc_get_json,
)

from .on_failure_helpers import on_wait_pods_appear_failure

logger = logging.getLogger(__name__)


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@entrypoint
def run(
    *,
    namespace: str,
    inference_service_manifest_path: str,
    gateway_status_address_name: str | None = "gateway-external",
    dry_run: bool = False,
    wait_long_scheduling: bool = False,
    deploy_monitor: bool = True,
) -> str:
    """
    Deploy an LLMInferenceService and wait for its endpoint.

    Args:
        namespace: Namespace used by llm_d
        inference_service_manifest_path: Path to the InferenceService YAML manifest file
        gateway_status_address_name: Gateway status address name for endpoint resolution
        dry_run: If True, only prepare the manifest without deploying
        wait_long_scheduling: If True, wait for all pods to be scheduled before checking service readiness with extended retry
        deploy_monitor: If True, deploy ServiceMonitor for monitoring the ISVC
    """

    ctx = execute_tasks(locals())

    if dry_run:
        return ctx.src_manifest_path

    # Ensure endpoint_url is available
    endpoint_url = getattr(ctx, "endpoint_url", None)
    if not endpoint_url:
        raise RuntimeError("Failed to resolve gateway endpoint URL after deployment")

    return endpoint_url


@task
def copy_manifest_to_src(args, ctx):
    """Copy inference service manifest to src directory and extract service name"""
    import shutil

    # Get the original manifest path
    original_path = Path(args.inference_service_manifest_path)

    # Load manifest to extract the service name
    manifest = load_yaml(original_path)
    ctx.inference_service_name = manifest["metadata"]["name"]

    # Validate that the service name is Kubernetes compliant
    normalized_name = slugify_identifier(ctx.inference_service_name)
    if normalized_name != ctx.inference_service_name:
        raise ValueError(
            f"LLMInferenceService name '{ctx.inference_service_name}' is not Kubernetes compliant. "
            f"Expected: '{normalized_name}'. "
            f"Names must be lowercase, contain only letters, numbers, and hyphens, "
            f"and be 63 characters or less."
        )

    ctx.selector = f"app.kubernetes.io/name={ctx.inference_service_name}"

    # Ensure the src directory exists
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Copy the manifest to src directory
    src_path = src_dir / original_path.name
    shutil.copy2(original_path, src_path)

    # Store the src path in context for other tasks to use
    ctx.src_manifest_path = str(src_path)

    return f"Copied manifest from {original_path} to {src_path} (service: {ctx.inference_service_name})"


@task
def check_dry_run(args, ctx):
    """Check if dry-run mode is enabled and return early if so"""
    if args.dry_run:
        return EarlyReturn(f"Dry-run completed: Prepared manifest for {ctx.inference_service_name}")
    return "Proceeding with full deployment"


@task
def delete_existing_service(args, ctx):
    """Delete existing LLMInferenceService"""

    name = ctx.inference_service_name
    oc(
        "delete",
        "llminferenceservice",
        name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
        check=False,
    )

    return f"Deleted existing LLMInferenceService {name}"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_old_pods_gone(args, ctx):
    """Wait for old llm-d pods to disappear"""

    result = oc(
        "get",
        "pods",
        "-n",
        args.namespace,
        "-l",
        ctx.selector,
        "--ignore-not-found=true",
        "--no-headers",
        check=False,
    )

    # Check if output is empty (no pods found)
    if not result.stdout.strip():
        return f"Old pods gone for {ctx.inference_service_name}"
    return False  # Retry


@task
def apply_inference_service(args, ctx):
    """Apply the LLMInferenceService manifest"""

    # Use the manifest copied to src directory
    src_manifest_path = ctx.src_manifest_path

    # Load and apply the manifest from src
    manifest = load_yaml(Path(src_manifest_path))
    oc_apply(src_manifest_path, manifest)
    return f"Applied LLMInferenceService manifest from {src_manifest_path} for {ctx.inference_service_name}"


@task
def deploy_servicemonitor(args, ctx):
    """Deploy ServiceMonitor for ISVC if deploy_monitor flag is enabled"""

    if not args.deploy_monitor:
        return "ServiceMonitor deployment disabled (deploy_monitor=False)"

    if args.dry_run:
        return "Dry-run, ServiceMonitor would be generated and applied"

    # Get ISVC name from context
    isvc_name = getattr(ctx, "inference_service_name", None)
    if not isvc_name:
        logger.warning("ISVC name not available, skipping ServiceMonitor deployment")
        return "Skipped: ISVC name not available"

    logger.info(f"Deploying ServiceMonitor for ISVC: {isvc_name}")

    # Get ISVC UID for owner reference
    owner_uid = _get_isvc_uid(isvc_name, args.namespace)
    if owner_uid:
        logger.info(f"Found ISVC UID: {owner_uid}")
    else:
        logger.warning("ISVC UID not found - ServiceMonitor will not have owner reference")

    # Render ServiceMonitor template using DSL template utilities
    # Add additional template variables to ctx for template access
    ctx.llmisvc_name = isvc_name
    ctx.owner_uid = owner_uid

    # Pass real args and ctx to template
    template_context = {
        "args": args,
        "ctx": ctx,
    }

    # Render the Jinja2 template (owner references are included in template)
    rendered_content = render_template("servicemonitor.yaml.j2", template_context)

    # Parse the rendered YAML content
    processed_manifests = list(yaml.safe_load_all(rendered_content))

    # Save manifests to artifacts
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    servicemonitor_path = artifacts_dir / "servicemonitor.yaml"

    with servicemonitor_path.open("w") as f:
        yaml.dump_all(processed_manifests, f, default_flow_style=False, sort_keys=False)

    # Apply to cluster
    oc("apply", "-f", str(servicemonitor_path))

    logger.info("✅ ServiceMonitor deployed successfully")
    return f"ServiceMonitor deployed for ISVC '{isvc_name}' and saved to {servicemonitor_path}"


@on_failure(on_wait_pods_appear_failure)
@retry(attempts=30, delay=5, backoff=1.0)
@task
def wait_pods_appear(args, ctx):
    """Wait for llm-d pods to appear"""

    # Use plain text output for better readability in logs
    result = oc(
        "get", "pods", "-n", args.namespace, "-l", ctx.selector, check=False, log_stdout=True
    )

    # Check if pods appeared (successful command with actual pod output)
    if (
        result.returncode == 0
        and result.stdout.strip()
        and "No resources found" not in result.stdout
    ):
        return f"Pods appeared for {ctx.inference_service_name}"
    return False  # Retry


@task
def query_service_status(args, ctx):
    """Query the status of the LLMInferenceService"""

    service_name = ctx.inference_service_name

    # Query only the Ready condition status
    result = oc(
        "get",
        "llminferenceservice",
        service_name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
        log_stdout=False,
    )

    ready_status = result.stdout.strip()
    ctx.is_ready = ready_status == "True"

    if ctx.is_ready:
        return f"LLMInferenceService {service_name} status: Ready"
    else:
        return f"LLMInferenceService {service_name} status: Not Ready"


@task
def query_service_message(args, ctx):
    """Query detailed message from LLMInferenceService"""

    service_name = ctx.inference_service_name

    # Query the Ready condition details
    result = oc(
        "get",
        "llminferenceservice",
        service_name,
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


def _check_pod_scheduling_status(args, ctx):
    """Helper function to check pod scheduling status and image pull errors"""
    service_name = ctx.inference_service_name

    # First, check if the LLMInferenceService exists
    llmisvc_result = oc(
        "get",
        "llminferenceservice",
        service_name,
        "-n",
        args.namespace,
        check=False,
    )

    if llmisvc_result.returncode != 0:
        return False, f"LLMInferenceService {service_name} does not exist yet"

    # Get pod status using plain text output
    result = oc(
        "get",
        "pods",
        "-l",
        ctx.selector,
        "-n",
        args.namespace,
        "--no-headers",
        check=False,
    )

    # Check for nonzero return code from pod query
    if result.returncode != 0:
        return False, f"Failed to query pods for service {service_name}"

    # Check if pod query result indicates no resources found
    if "No resources found" in result.stdout:
        return False, "No pods found for the service yet"

    if not result.stdout.strip():
        return False, "No pods found for the service yet"

    # Check for image pull errors and fail early if found
    image_pull_result = oc(
        "get",
        "pods",
        "-l",
        ctx.selector,
        "-n",
        args.namespace,
        "--no-headers",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}:{range .status.containerStatuses[*]}{.state.waiting.reason}{'|'}{end}{'\\n'}{end}",
        check=False,
    )

    for line in image_pull_result.stdout.strip().split("\n"):
        pod_name, waiting_reasons = line.split(":", 1)
        if not waiting_reasons:
            continue
        reasons = waiting_reasons.split("|")
        for reason in reasons:
            if reason in ("ImagePullBackOff", "ErrImagePull"):
                raise RuntimeError(
                    f"Pod {pod_name} has image pull error: {reason}. Aborting wait due to image pull failure."
                )

    # Keep waiting if any pod is Pending or SchedulingGated
    if "Pending" in result.stdout:
        return False, "Waiting for pods to exit Pending state"

    if "SchedulingGated" in result.stdout:
        return False, "Waiting for pods to exit SchedulingGated state"

    return f"All pods for {service_name} are scheduled successfully"


@retry(attempts=12, delay=10, backoff=1.0)
@task
def wait_pods_scheduled_short(args, ctx):
    """Wait for all pods to be scheduled with short retry (normal scheduling)"""

    # Check if this task is enabled
    if args.wait_long_scheduling:
        return "Short pod scheduling wait disabled - long scheduling is enabled"

    return _check_pod_scheduling_status(args, ctx)


@retry(attempts=999999, delay=30, backoff=1.0)
@task
def wait_pods_scheduled_long(args, ctx):
    """Wait for all pods to be scheduled with extended retry (long scheduling)"""

    # Check if this task is enabled
    if not args.wait_long_scheduling:
        return "Long pod scheduling wait disabled by parameter"

    return _check_pod_scheduling_status(args, ctx)


@retry(attempts=180, delay=10, backoff=1.0)
@task
def wait_service_ready(args, ctx):
    """Wait for LLMInferenceService to be ready"""

    service_name = ctx.inference_service_name

    # Query the current status and show diagnostic info
    result = oc(
        "get",
        "llminferenceservice",
        service_name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.status.conditions[?(@.type=='Ready')]}",
    )

    # Also show pod status for debugging
    oc(
        "get",
        "pods",
        "-l",
        ctx.selector,
        "-n",
        args.namespace,
    )

    # Check for pod restarts and abort if any pods have restarted
    restart_result = oc(
        "get",
        "pods",
        "-l",
        ctx.selector,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}:{.status.containerStatuses[*].restartCount}{'\\n'}{end}",
        log_stdout=False,
    )

    if restart_result.stdout.strip():
        for line in restart_result.stdout.strip().split("\n"):
            if ":" in line:
                pod_name, restart_counts = line.split(":", 1)
                # Check if any container has restarted
                counts = restart_counts.split()
                for count_str in counts:
                    try:
                        if int(count_str) > 0:
                            raise RuntimeError(
                                f"Pod {pod_name} has restarted (restart count: {count_str}). Aborting wait due to pod restart."
                            )
                    except ValueError:
                        # Skip non-numeric restart counts
                        pass

    if result.stdout.strip():
        try:
            import json

            condition = json.loads(result.stdout)
            status = condition.get("status", "Unknown")
            reason = condition.get("reason", "Unknown")
            message = condition.get("message", "No message")

            if status == "True":
                return f"LLMInferenceService {service_name} is ready"
            else:
                return (
                    False,
                    f"Service not ready - Status: {status}, Reason: {reason}, Message: {message}",
                )

        except (json.JSONDecodeError, KeyError) as e:
            return (False, f"Failed to parse Ready condition: {e}")
    else:
        return (False, f"No Ready condition found in status for {service_name}")


def try_resolve_endpoint_url(
    *, namespace: str, inference_service_name: str, gateway_status_address_name: str | None
) -> str | None:
    logger.info(
        f"=== Resolving endpoint URL for {inference_service_name} in namespace {namespace} ==="
    )
    logger.info(f"Target gateway_status_address_name: {gateway_status_address_name}")

    payload = oc_get_json("llminferenceservice", name=inference_service_name, namespace=namespace)

    # Log the entire status section for debugging
    status = payload.get("status", {})
    logger.info(f"Status section keys found: {list(status.keys())}")

    # Check status.address first
    status_address = status.get("address")
    logger.info(f"status.address content: {status_address}")

    if status_address:
        logger.info("✓ status.address exists")
        if isinstance(status_address, dict):
            logger.info("✓ status.address is a dict")
            if status_address.get("url"):
                url = status_address["url"]
                logger.info(f"✓ Found URL in status.address: '{url}'")

                # When gateway_status_address_name is None, append port 8000 if needed
                if gateway_status_address_name is None:
                    logger.info("Mode: No gateway (will append port 8000 if needed)")
                    if ":" not in url.split("/")[-1]:  # Check if no port in the hostname part
                        url = f"{url}:8000"
                        logger.info(f"✓ Appended port 8000: '{url}'")
                    else:
                        logger.info("✓ URL already has port, using as-is")
                else:
                    logger.info(
                        f"Mode: Gateway '{gateway_status_address_name}' (no port modification)"
                    )

                logger.info(f"🎯 RESOLVED from status.address: '{url}'")
                return url
            else:
                logger.info("✗ status.address has no 'url' field")
                logger.info(f"  Available fields: {list(status_address.keys())}")
        else:
            logger.info(f"✗ status.address is not a dict, type: {type(status_address)}")
    else:
        logger.info("✗ No status.address found")

    # Fallback to existing status.addresses logic
    logger.info("--- Falling back to status.addresses lookup ---")
    status_addresses = status.get("addresses", [])
    logger.info(f"status.addresses content: {status_addresses}")

    if not status_addresses:
        logger.info("✗ No status.addresses found - RESOLUTION FAILED")
        return None

    logger.info(f"✓ Found {len(status_addresses)} address(es) in status.addresses")

    for i, address in enumerate(status_addresses):
        logger.info(f"Checking address[{i}]: {address}")

        # When gateway_status_address_name is None, return the first address with a URL and append port 8000
        if gateway_status_address_name is None:
            logger.info("  Mode: No gateway filter (looking for any URL)")
            if address.get("url"):
                url = address["url"]
                logger.info(f"  ✓ Found URL in address[{i}]: '{url}'")
                # Append port 8000 when not using gateway if no port is already specified
                if ":" not in url.split("/")[-1]:  # Check if no port in the hostname part
                    url = f"{url}:8000"
                    logger.info(f"  ✓ Appended port 8000: '{url}'")
                else:
                    logger.info("  ✓ URL already has port, using as-is")

                logger.info(f"🎯 RESOLVED from status.addresses[{i}]: '{url}'")
                return url
            else:
                logger.info(f"  ✗ Address[{i}] has no 'url' field")
                logger.info(f"    Available fields: {list(address.keys())}")
        # Otherwise, match by name
        else:
            address_name = address.get("name")
            logger.info(
                f"  Mode: Gateway filter (looking for name='{gateway_status_address_name}')"
            )
            logger.info(f"  Address[{i}] name: '{address_name}'")

            if address_name == gateway_status_address_name:
                logger.info(f"  ✓ Name matches '{gateway_status_address_name}'")
                if address.get("url"):
                    url = address["url"]
                    logger.info(f"  ✓ Found URL: '{url}'")
                    logger.info(f"🎯 RESOLVED from status.addresses[{i}] by name: '{url}'")
                    return url
                else:
                    logger.info("  ✗ Matching address has no 'url' field")
                    logger.info(f"    Available fields: {list(address.keys())}")
            else:
                logger.info(
                    f"  ✗ Name mismatch: '{address_name}' != '{gateway_status_address_name}'"
                )

    logger.info("❌ RESOLUTION FAILED - No suitable URL found in any address")
    return None


@retry(attempts=30, delay=10, backoff=1.0)
@task
def resolve_endpoint_task(args, ctx):
    """Resolve the gateway endpoint URL"""

    endpoint_url = try_resolve_endpoint_url(
        namespace=args.namespace,
        inference_service_name=ctx.inference_service_name,
        gateway_status_address_name=args.gateway_status_address_name,
    )
    if endpoint_url:
        ctx.endpoint_url = endpoint_url
        write_text(args.artifact_dir / "artifacts" / "endpoint.url", f"{endpoint_url}\n")
        return f"Endpoint resolved: {endpoint_url}"
    return False, "No endpoint URL available"


@always
@task
def capture_llmisv_description(args, ctx):
    """Capture LLMISV description with events and status for failure analysis"""

    if args.dry_run:
        return "Dry-run, nothing to do"

    try:
        # Ensure artifacts directory exists
        artifacts_dir = args.artifact_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Use LLMInferenceService name from context
        service_name = getattr(ctx, "inference_service_name", None)
        if not service_name:
            return "No service name available"

        # Capture LLMISV description
        result = oc(
            "describe",
            "llminferenceservice",
            service_name,
            "-n",
            args.namespace,
            log_stdout=False,
            check=False,
        )

        llmisv_desc_path = artifacts_dir / "llmisv_description.txt"
        with open(llmisv_desc_path, "w", encoding="utf-8") as f:
            f.write(result.stdout)

        return f"Captured LLMISV description to {llmisv_desc_path}"

    except Exception as e:
        return f"Failed to capture LLMISV description: {e}"


@always
@task
def capture_replicaset_description(args, ctx):
    """Capture ReplicaSet description for pod creation failure analysis"""

    if args.dry_run:
        return "Dry-run, nothing to do"

    try:
        # Ensure artifacts directory exists
        artifacts_dir = args.artifact_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Use LLMInferenceService name from context
        service_name = getattr(ctx, "inference_service_name", None)
        if not service_name:
            return "No service name available"

        # Get replicasets for the service
        rs_result = oc(
            "get",
            "replicaset",
            "-l",
            ctx.selector,
            "-n",
            args.namespace,
            "-o",
            "name",
            log_stdout=False,
            check=False,
        )

        replicaset_descriptions = []

        if rs_result.stdout.strip():
            # Describe each replicaset
            for rs_name in rs_result.stdout.strip().split("\n"):
                if not rs_name.strip():
                    continue

                rs_desc_result = oc(
                    "describe",
                    rs_name.strip(),
                    "-n",
                    args.namespace,
                    log_stdout=False,
                    check=False,
                )
                replicaset_descriptions.append(rs_desc_result.stdout)

        # Save all replicaset descriptions
        rs_desc_path = artifacts_dir / "replicaset_description.txt"
        with open(rs_desc_path, "w", encoding="utf-8") as f:
            if replicaset_descriptions:
                f.write("\n".join(replicaset_descriptions))
            else:
                f.write("No replicasets found for the service")

        return f"Captured ReplicaSet description to {rs_desc_path}"

    except Exception as e:
        return f"Failed to capture ReplicaSet description: {e}"


@always
@task
def capture_final_llmisvc_yaml(args, ctx):
    """Capture the final YAML state of the LLMInferenceService"""

    if args.dry_run:
        return "Dry-run, nothing to do"

    try:
        # Ensure artifacts directory exists
        artifacts_dir = args.artifact_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Use LLMInferenceService name from context
        service_name = getattr(ctx, "inference_service_name", None)
        if not service_name:
            return "No service name available"

        # Capture final YAML state
        result = oc(
            "get",
            "llminferenceservice",
            service_name,
            "-n",
            args.namespace,
            "-o",
            "yaml",
            log_stdout=False,
            check=False,
        )

        llmisvc_yaml_path = artifacts_dir / "llmisvc_final.yaml"

        if result.returncode != 0:
            # Handle the case where the LLMInferenceService is not found
            if "not found" in result.stderr.lower():
                logger.warning(
                    f"LLMInferenceService '{service_name}' not found in namespace '{args.namespace}'"
                )
                with open(llmisvc_yaml_path, "w", encoding="utf-8") as f:
                    f.write(
                        f"# LLMInferenceService '{service_name}' not found in namespace '{args.namespace}'\n"
                    )
                    f.write(f"# Error: {result.stderr.strip()}\n")
                return f"LLMInferenceService not found, logged error to {llmisvc_yaml_path}"
            else:
                # Handle other error cases
                logger.error(
                    f"Failed to get LLMInferenceService '{service_name}': {result.stderr.strip()}"
                )
                with open(llmisvc_yaml_path, "w", encoding="utf-8") as f:
                    f.write(f"# Error getting LLMInferenceService '{service_name}'\n")
                    f.write(f"# Error: {result.stderr.strip()}\n")
                return f"Error getting LLMInferenceService, logged error to {llmisvc_yaml_path}"

        # Success case - write the YAML content
        with open(llmisvc_yaml_path, "w", encoding="utf-8") as f:
            f.write(result.stdout)

        return f"Captured final LLMInferenceService YAML to {llmisvc_yaml_path}"

    except Exception as e:
        return f"Failed to capture final LLMInferenceService YAML: {e}"


@always
@task
def capture_workload_overview(args, ctx):
    """Capture deployment, replicaset, and pod overview for debugging"""

    if args.dry_run:
        return "Dry-run, nothing to do"

    # Ensure artifacts directory exists
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Use selector from context
    selector = getattr(ctx, "selector", None)
    if not selector:
        return "No selector available"

    workload_overview_path = artifacts_dir / "workload_overview.txt"

    # Capture deployment, replicaset, and pod overview
    oc(
        "get",
        "deploy,rs,pod",
        "-l",
        selector,
        "-n",
        args.namespace,
        "-o",
        "wide",
        check=False,
        stdout_dest=workload_overview_path,
    )

    return f"Captured workload overview to {workload_overview_path}"


@always
@task
def capture_pod_status(args, ctx):
    """Capture pod status for debugging"""

    if args.dry_run:
        return "Dry-run, nothing to do"

    try:
        # Ensure artifacts directory exists
        artifacts_dir = args.artifact_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Use selector from context
        selector = getattr(ctx, "selector", None)
        if not selector:
            return "No selector available"

        # Capture pod status with wide output
        result = oc(
            "get",
            "pods",
            "-l",
            selector,
            "-n",
            args.namespace,
            "-o",
            "wide",
            log_stdout=False,
            check=False,
        )

        pod_status_path = artifacts_dir / "pod_status.txt"
        with open(pod_status_path, "w", encoding="utf-8") as f:
            f.write(result.stdout)

        return f"Captured pod status to {pod_status_path}"

    except Exception as e:
        return f"Failed to capture pod status: {e}"


@always
@task
def capture_pod_descriptions(args, ctx):
    """Capture pod descriptions for debugging"""

    if args.dry_run:
        return "Dry-run, nothing to do"

    try:
        # Ensure artifacts directory exists
        artifacts_dir = args.artifact_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Use selector from context
        selector = getattr(ctx, "selector", None)
        if not selector:
            return "No selector available"

        # Get pod names
        pod_result = oc(
            "get",
            "pods",
            "-l",
            selector,
            "-n",
            args.namespace,
            "-o",
            "jsonpath={.items[*].metadata.name}",
            log_stdout=False,
            check=False,
        )

        pod_names = pod_result.stdout.strip().split()
        if not pod_names or not pod_result.stdout.strip():
            pod_desc_path = artifacts_dir / "pod_descriptions.txt"
            with open(pod_desc_path, "w", encoding="utf-8") as f:
                f.write("No pods found for the service")
            return f"No pods found, wrote empty file to {pod_desc_path}"

        # Describe each pod
        pod_descriptions = []
        for pod_name in pod_names:
            describe_result = oc(
                "describe",
                "pod",
                pod_name,
                "-n",
                args.namespace,
                log_stdout=False,
                check=False,
            )
            pod_descriptions.append(
                f"=== Description for pod: {pod_name} ===\n{describe_result.stdout}"
            )

        # Save all pod descriptions
        pod_desc_path = artifacts_dir / "pod_descriptions.txt"
        with open(pod_desc_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(pod_descriptions))

        return f"Captured descriptions for {len(pod_names)} pods to {pod_desc_path}"

    except Exception as e:
        return f"Failed to capture pod descriptions: {e}"


@always
@task
def capture_pod_yaml(args, ctx):
    """Capture pod YAML definitions for debugging"""

    if args.dry_run:
        return "Dry-run, nothing to do"

    try:
        # Ensure artifacts directory exists
        artifacts_dir = args.artifact_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Use selector from context
        selector = getattr(ctx, "selector", None)
        if not selector:
            return "No selector available"

        # Capture all pod YAMLs
        result = oc(
            "get",
            "pods",
            "-l",
            selector,
            "-n",
            args.namespace,
            "-o",
            "yaml",
            log_stdout=False,
            check=False,
        )

        pod_yaml_path = artifacts_dir / "pod_definitions.yaml"
        with open(pod_yaml_path, "w", encoding="utf-8") as f:
            f.write(result.stdout)

        return f"Captured pod YAML definitions to {pod_yaml_path}"

    except Exception as e:
        return f"Failed to capture pod YAML: {e}"


def _get_isvc_uid(isvc_name: str, namespace: str) -> str | None:
    """Get ISVC UID for owner reference using oc command."""
    try:
        result = oc(
            "get",
            "llminferenceservice",
            isvc_name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.metadata.uid}",
            check=False,
        )

        if result.returncode != 0:
            logger.warning(
                f"Failed to get ISVC UID: oc command failed with return code {result.returncode}"
            )
            return None

        uid = result.stdout.strip()
        return uid if uid else None
    except Exception as e:
        logger.warning(f"Failed to get ISVC UID: {e}")
        return None


if __name__ == "__main__":
    run.main()
