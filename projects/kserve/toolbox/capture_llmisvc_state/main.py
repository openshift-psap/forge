#!/usr/bin/env python3

"""
LLMInferenceService state capture using task-based DSL
Replaces llmd_capture_llmisvc_state Ansible role
"""

import json
import logging

import yaml

from projects.core.dsl import entrypoint, execute_tasks, shell, task
from projects.core.dsl.utils.k8s import oc, oc_get_json

logger = logging.getLogger(__name__)


@entrypoint
def run(
    llmisvc_name: str,
    *,
    namespace: str = "",
    gateway_name: str = "",
    gateway_namespace: str = "",
    since_time: str = "",
):
    """
    Capture LLMInferenceService state using task-based DSL

    Args:
        llmisvc_name: Name of the LLMInferenceService to capture
        namespace: Namespace of the LLMInferenceService (empty string auto-detects current namespace)
        gateway_name: Gateway to capture when provided
        gateway_namespace: Namespace of the Gateway
        since_time: Only capture Gateway logs since this ISO 8601 time, when provided
    """

    if not llmisvc_name:
        raise ValueError("llmisvc_name must not be empty")

    return execute_tasks(locals())


@task
def setup_directories(args, context):
    """Create the artifacts directory"""

    shell.mkdir("artifacts")
    shell.mkdir("artifacts/logs")
    return "Artifacts directory created"


@task
def get_current_timestamp(args, context):
    """Get current timestamp"""

    result = shell.run("date -Iseconds")
    context.capture_timestamp = result.stdout.strip()
    return f"Timestamp: {context.capture_timestamp}"


@task
def determine_target_namespace(args, context):
    """Get current namespace if not specified"""
    if args.namespace:
        context.target_namespace = args.namespace
        return f"Using specified namespace: {context.target_namespace}"

    result = shell.run("oc project -q")
    context.target_namespace = result.stdout.strip()
    return f"Using current namespace: {context.target_namespace}"


@task
def capture_llminferenceservice_yaml(args, context):
    """Capture the LLMInferenceService definition"""
    shell.run(
        f"oc get llminferenceservice {args.llmisvc_name} -n {context.target_namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.yaml",
        check=False,
    )
    return "LLMInferenceService YAML captured"


@task
def capture_llminferenceservice_json(args, context):
    """Capture LLMInferenceService status in JSON for easier parsing"""
    shell.run(
        f"oc get llminferenceservice {args.llmisvc_name} -n {context.target_namespace} -ojson",
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.json",
        check=False,
    )
    return "LLMInferenceService JSON captured"


@task
def capture_related_pods_yaml(args, context):
    """Capture all pods related to the LLMInferenceService"""
    shell.run(
        f'oc get pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -oyaml',
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.pods.yaml",
        check=False,
    )
    return "Related pods YAML captured"


@task
def capture_related_deployments(args, context):
    """Capture deployments related to the LLMInferenceService"""
    shell.run(
        f'oc get deployments -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -oyaml',
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.deployments.yaml",
        check=False,
    )
    return "Related deployments captured"


@task
def capture_related_deployments_json(args, context):
    """Capture deployments related to the LLMInferenceService in JSON format for easier parsing"""
    shell.run(
        f'oc get deployments -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -ojson',
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.deployments.json",
        check=False,
    )
    return "Related deployments JSON captured"


@task
def capture_related_replicasets(args, context):
    """Capture replicasets related to the LLMInferenceService"""
    shell.run(
        f'oc get replicasets -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -oyaml',
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.replicasets.yaml",
        check=False,
    )
    return "Related replicasets captured"


@task
def capture_namespace_pods(args, context):
    """Capture all pods in the namespace with wide output"""
    shell.run(
        f"oc get pods -owide -n {context.target_namespace}",
        stdout_dest=args.artifact_dir / "artifacts/namespace.pods.status.txt",
        check=False,
    )
    return "Namespace pods status captured"


@task
def capture_namespace_services(args, context):
    """Capture all services in the namespace"""
    shell.run(
        f"oc get svc -n {context.target_namespace}",
        stdout_dest=args.artifact_dir / "artifacts/namespace.services.status.txt",
        check=False,
    )
    return "Namespace services captured"


@task
def capture_servicemonitors(args, context):
    """Capture ServiceMonitors for monitoring"""
    shell.run(
        f'oc get servicemonitor -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -oyaml',
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.servicemonitors.yaml",
        check=False,
    )
    return "ServiceMonitors captured"


@task
def capture_podmonitors(args, context):
    """Capture PodMonitors for monitoring"""
    shell.run(
        f'oc get podmonitor -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -oyaml',
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.podmonitors.yaml",
        check=False,
    )
    return "PodMonitors captured"


@task
def capture_kserve_objects(args, context):
    """Capture KServe objects in YAML format (ignoring errors)"""

    # Capture ServingRuntimes (namespaced)
    shell.run(
        f"oc get servingruntimes -n {context.target_namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/kserve.servingruntimes.yaml",
        check=False,
    )

    # Capture ClusterServingRuntimes (cluster-scoped)
    shell.run(
        "oc get clusterservingruntimes -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/kserve.clusterservingruntimes.yaml",
        check=False,
    )

    # Capture InferenceServices (if any exist alongside LLMInferenceServices)
    shell.run(
        f"oc get inferenceservices -n {context.target_namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/kserve.inferenceservices.yaml",
        check=False,
    )

    # Capture KServe MutatingWebhookConfigurations
    shell.run(
        'oc get mutatingwebhookconfigurations -l "app.kubernetes.io/part-of=kserve" -oyaml',
        stdout_dest=args.artifact_dir / "artifacts/kserve.mutatingwebhooks.yaml",
        check=False,
    )

    # Capture KServe ValidatingWebhookConfigurations
    shell.run(
        'oc get validatingwebhookconfigurations -l "app.kubernetes.io/part-of=kserve" -oyaml',
        stdout_dest=args.artifact_dir / "artifacts/kserve.validatingwebhooks.yaml",
        check=False,
    )

    return "KServe objects captured (errors ignored)"


def _capture_pod_container_logs(
    args, context, output_dir, file_suffix="", oc_flags="", description="logs"
):
    """
    Helper function to capture logs from LLMInferenceService pods - one file per container.

    Args:
        args: Task arguments
        context: Task context
        output_dir: Directory to save log files
        file_suffix: Suffix for log files (e.g., ".previous")
        oc_flags: Additional flags for oc logs command (e.g., "--previous")
        description: Description for return message

    Returns:
        Tuple of (captured_pod_count, total_non_empty_files)
    """
    result = shell.run(
        f'oc get pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
        log_stdout=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return 0, 0

    captured_count = 0
    total_files = 0

    for pod_name in pod_names:
        # Get container names for this pod
        container_result = shell.run(
            f'oc get pod {pod_name} -n {context.target_namespace} -o jsonpath="{{.spec.containers[*].name}}"',
            check=False,
            log_stdout=False,
        )

        container_names = container_result.stdout.strip().split()
        if not container_names:
            continue

        for container_name in container_names:
            log_file = output_dir / f"{pod_name}-{container_name}{file_suffix}.log"

            # Build oc logs command
            cmd = f"oc logs {pod_name} -c {container_name} -n {context.target_namespace}"
            if oc_flags:
                cmd += f" {oc_flags}"

            shell.run(cmd, stdout_dest=log_file, check=False)

            # Check if log file is too small and delete if so
            if log_file.exists() and log_file.stat().st_size < 10:
                log_file.unlink()
            else:
                total_files += 1

        captured_count += 1

    return captured_count, total_files


@task
def capture_pod_logs(args, context):
    """Capture logs from LLMInferenceService pods - one file per container"""
    logs_dir = args.artifact_dir / "artifacts/logs"

    captured_count, total_files = _capture_pod_container_logs(
        args, context, logs_dir, description="current logs"
    )

    if captured_count == 0:
        return "No pods found to capture logs"

    return (
        f"Pod logs captured for {captured_count} pods ({total_files} non-empty container log files)"
    )


@task
def capture_pod_previous_logs(args, context):
    """Capture previous logs from LLMInferenceService pods if available - one file per container"""
    # Create previous logs subdirectory
    previous_logs_dir = args.artifact_dir / "artifacts/logs/previous"
    shell.mkdir("artifacts/logs/previous")

    captured_count, total_files = _capture_pod_container_logs(
        args,
        context,
        previous_logs_dir,
        file_suffix=".previous",
        oc_flags="--previous",
        description="previous logs",
    )

    if captured_count == 0:
        return "No pods found to capture previous logs"

    return f"Pod previous logs captured for {captured_count} pods ({total_files} non-empty container log files in logs/previous/)"


@task
def capture_llminferenceservice_describe(args, context):
    """Capture describe output for the LLMInferenceService"""
    shell.run(
        f"oc describe llminferenceservice {args.llmisvc_name} -n {context.target_namespace}",
        stdout_dest=args.artifact_dir / "artifacts/llminferenceservice.describe.txt",
        check=False,
    )
    return "LLMInferenceService describe captured"


@task
def capture_workload_overview(args, context):
    """Capture deployment, replicaset, and pod overview for debugging"""

    workload_overview_path = args.artifact_dir / "artifacts/workload_overview.txt"

    # Capture deployment, replicaset, and pod overview with wide output
    shell.run(
        f'oc get deploy,rs,pod -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -o wide',
        stdout_dest=workload_overview_path,
        check=False,
    )

    return f"Captured workload overview to {workload_overview_path}"


@task
def capture_workload_descriptions(args, context):
    """Capture workload descriptions for deployments, replicasets, and pods in a single file"""

    descriptions_file = args.artifact_dir / "artifacts/workload_descriptions.txt"

    with open(descriptions_file, "w") as handle:
        # Capture deployments descriptions
        handle.write("=== DEPLOYMENTS ===\n")
        deploy_result = shell.run(
            f'oc describe deployments -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace}',
            log_stdout=False,
            check=False,
        )
        handle.write(deploy_result.stdout)
        handle.write("\n\n")

        # Capture replicasets descriptions
        handle.write("=== REPLICASETS ===\n")
        rs_result = shell.run(
            f'oc describe replicasets -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace}',
            log_stdout=False,
            check=False,
        )
        handle.write(rs_result.stdout)
        handle.write("\n\n")

        # Capture pods descriptions
        handle.write("=== PODS ===\n")
        pods_result = shell.run(
            f'oc describe pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace}',
            log_stdout=False,
            check=False,
        )
        handle.write(pods_result.stdout)
        handle.write("\n")

    return f"Captured workload descriptions to {descriptions_file}"


@task
def capture_pods_describe(args, context):
    """Capture describe output for related pods"""
    result = shell.run(
        f'oc get pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return "No pods found to describe"

    describe_file = args.artifact_dir / "artifacts/llminferenceservice.pods.describe.txt"

    with open(describe_file, "w") as handle:
        for pod_name in pod_names:
            handle.write(f"=== Describe for pod: {pod_name} ===\n")
            describe_result = shell.run(
                f"oc describe pod {pod_name} -n {context.target_namespace}",
                log_stdout=False,
                check=False,
            )
            handle.write(describe_result.stdout)
            handle.write("\n")

    return f"Pod describe output captured for {len(pod_names)} pods"


@task
def determine_used_nodes(args, context):
    """Determine which nodes are used by LLMInferenceService pods"""
    result = shell.run(
        f'oc get pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -o jsonpath="{{.items[*].spec.nodeName}}"',
        check=False,
        log_stdout=False,
    )

    node_names = list(set(result.stdout.strip().split()))  # Remove duplicates
    if not node_names or not result.stdout.strip():
        context.used_nodes = []
        return "No nodes found for LLMInferenceService pods"

    context.used_nodes = [node for node in node_names if node]  # Filter out empty strings
    return f"Found {len(context.used_nodes)} nodes used by LLMInferenceService pods: {', '.join(context.used_nodes)}"


@task
def capture_pod_node_mapping(args, context):
    """Capture pod->node mapping as YAML"""
    result = shell.run(
        f'oc get pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName --no-headers',
        check=False,
        log_stdout=False,
    )

    if not result.stdout.strip():
        return "No pod->node mapping found"

    mapping_file = args.artifact_dir / "artifacts/pod_node_mapping.yaml"
    pod_node_mapping = {}

    for line in result.stdout.strip().split("\n"):
        if line.strip():
            parts = line.split()
            if len(parts) >= 2:
                pod_name = parts[0]
                node_name = parts[1] if parts[1] != "<none>" else None
                pod_node_mapping[pod_name] = node_name

    import yaml

    with open(mapping_file, "w") as f:
        yaml.safe_dump(
            {"pod_node_mapping": pod_node_mapping, "capture_timestamp": context.capture_timestamp},
            f,
            default_flow_style=False,
        )

    return f"Pod->node mapping captured to {mapping_file} ({len(pod_node_mapping)} pods)"


@task
def capture_node_gpu_mapping(args, context):
    """Capture node->GPU type mapping as YAML"""
    if not context.used_nodes:
        return "No nodes to capture GPU mapping for"

    node_gpu_mapping = {}

    for node_name in context.used_nodes:
        # Try nvidia.com/gpu.product first
        result = shell.run(
            f'oc get node {node_name} -o jsonpath="{{.metadata.labels.nvidia\\.com/gpu\\.product}}"',
            check=False,
            log_stdout=False,
        )
        gpu_type = result.stdout.strip()

        # If not found, try gpu.nvidia.com/class as fallback
        if not gpu_type:
            result = shell.run(
                f'oc get node {node_name} -o jsonpath="{{.metadata.labels.gpu\\.nvidia\\.com/class}}"',
                check=False,
                log_stdout=False,
            )
            gpu_class = result.stdout.strip()
            if gpu_class:
                gpu_type = f"NVIDIA-{gpu_class}"

        # If still not found, try nvidia.com/gpu.accelerator as fallback
        if not gpu_type:
            result = shell.run(
                f'oc get node {node_name} -o jsonpath="{{.metadata.labels.nvidia\\.com/gpu\\.accelerator}}"',
                check=False,
                log_stdout=False,
            )
            gpu_accelerator = result.stdout.strip()
            if gpu_accelerator:
                gpu_type = f"NVIDIA-{gpu_accelerator.upper()}"

        # Final fallback
        if not gpu_type:
            gpu_type = "unknown"

        node_gpu_mapping[node_name] = gpu_type

    mapping_file = args.artifact_dir / "artifacts/node_gpu_mapping.yaml"

    import yaml

    with open(mapping_file, "w") as f:
        yaml.safe_dump(
            {"node_gpu_mapping": node_gpu_mapping, "capture_timestamp": context.capture_timestamp},
            f,
            default_flow_style=False,
        )

    return f"Node->GPU mapping captured to {mapping_file} ({len(node_gpu_mapping)} nodes)"


@task
def capture_used_nodes_yaml(args, context):
    """Capture YAML definitions for nodes used by LLMInferenceService pods"""
    if not context.used_nodes:
        return "No nodes to capture YAML for"

    nodes_dir = args.artifact_dir / "artifacts/nodes"
    shell.mkdir("artifacts/nodes")

    captured_count = 0
    for node_name in context.used_nodes:
        node_file = nodes_dir / f"{node_name}.yaml"
        shell.run(
            f"oc get node {node_name} -oyaml",
            stdout_dest=node_file,
            check=False,
        )
        captured_count += 1

    return f"Captured YAML for {captured_count} nodes in {nodes_dir}"


@task
def capture_routing_resources(args, context):
    """Capture this service's HTTPRoutes and InferencePools, with their status."""
    artifacts = args.artifact_dir / "artifacts"
    context.routing_capture_warnings = []
    selector = f"app.kubernetes.io/name={args.llmisvc_name}"
    resources = (
        ("httproutes.gateway.networking.k8s.io", "http_routes.yaml"),
        ("inferencepools.inference.networking.k8s.io", "inference_pools_v1.yaml"),
        ("inferencepools.inference.networking.x-k8s.io", "inference_pools_v1alpha2.yaml"),
    )
    pool_found = False
    for kind, filename in resources:
        result = oc(
            "get",
            kind,
            "-n",
            context.target_namespace,
            "-l",
            selector,
            "-o",
            "yaml",
            check=False,
            log_stdout=False,
            log_stderr=False,
        )
        if result.returncode == 0:
            (artifacts / filename).write_text(result.stdout, encoding="utf-8")
            items = (yaml.safe_load(result.stdout) or {}).get("items", [])
            if kind.startswith("inferencepools."):
                pool_found |= bool(items)
            elif not items:
                context.routing_capture_warnings.append(
                    f"No HTTPRoutes found for {args.llmisvc_name}"
                )
        elif not kind.startswith("inferencepools."):
            context.routing_capture_warnings.append(
                f"Could not capture {kind}: {result.stderr.strip()}"
            )

    if not pool_found:
        context.routing_capture_warnings.append(f"No InferencePools found for {args.llmisvc_name}")

    if args.gateway_name:
        result = oc(
            "get",
            "gateways.gateway.networking.k8s.io",
            args.gateway_name,
            "-n",
            args.gateway_namespace,
            "-o",
            "yaml",
            check=False,
            log_stdout=False,
            log_stderr=False,
        )
        if result.returncode == 0:
            (artifacts / "gateway.yaml").write_text(result.stdout, encoding="utf-8")
        else:
            context.routing_capture_warnings.append(
                f"Could not capture Gateway: {result.stderr.strip()}"
            )

    return "Captured routing YAML, including resource status"


@task
def capture_gateway_pod_logs(args, context):
    """Capture Gateway data-plane pod logs using the Gateway Service selectors."""
    if not args.gateway_name:
        return "No Gateway configured for log capture"

    artifacts = args.artifact_dir / "artifacts"
    services_result = oc(
        "get",
        "service",
        "-n",
        args.gateway_namespace,
        "-o",
        "json",
        check=False,
        log_stdout=False,
        log_stderr=False,
    )
    if services_result.returncode != 0:
        context.routing_capture_warnings.append(
            f"Could not list Gateway Services: {services_result.stderr.strip()}"
        )
        return "Gateway Service discovery failed"

    services = [
        service
        for service in json.loads(services_result.stdout)["items"]
        if service.get("spec", {}).get("selector", {}).get("gateway.networking.k8s.io/gateway-name")
        == args.gateway_name
    ]
    (artifacts / "gateway.services.yaml").write_text(
        yaml.safe_dump({"items": services}, sort_keys=False), encoding="utf-8"
    )
    if not services:
        context.routing_capture_warnings.append("No Services found for the configured Gateway")

    seen_pods = set()
    for service in services:
        service_namespace = service["metadata"]["namespace"]
        selector = service.get("spec", {}).get("selector", {})
        if not selector:
            context.routing_capture_warnings.append(
                f"Gateway Service {service_namespace}/{service['metadata']['name']} has no pod selector"
            )
            continue

        pods_result = oc(
            "get",
            "pod",
            "-n",
            service_namespace,
            "-l",
            ",".join(f"{key}={value}" for key, value in selector.items()),
            "-o",
            "json",
            check=False,
            log_stdout=False,
            log_stderr=False,
        )
        if pods_result.returncode != 0:
            context.routing_capture_warnings.append(
                f"Could not list Gateway pods in {service_namespace}: {pods_result.stderr.strip()}"
            )
            continue

        for pod in json.loads(pods_result.stdout)["items"]:
            pod_name = pod["metadata"]["name"]
            pod_key = (service_namespace, pod_name)
            if pod_key in seen_pods:
                continue
            seen_pods.add(pod_key)
            pod_dir = artifacts / "gateway-pods"
            pod_dir.mkdir(exist_ok=True)
            (pod_dir / f"{service_namespace}.{pod_name}.yaml").write_text(
                yaml.safe_dump(pod, sort_keys=False), encoding="utf-8"
            )
            for container in pod["spec"]["containers"]:
                container_name = container["name"]
                logs = oc(
                    "logs",
                    pod_name,
                    "-n",
                    service_namespace,
                    "-c",
                    container_name,
                    "--timestamps",
                    *(["--since-time", args.since_time] if args.since_time else []),
                    check=False,
                    log_stdout=False,
                    log_stderr=False,
                )
                if logs.returncode == 0:
                    logs_dir = artifacts / "gateway-logs"
                    logs_dir.mkdir(exist_ok=True)
                    (logs_dir / f"{service_namespace}.{pod_name}.{container_name}.log").write_text(
                        logs.stdout, encoding="utf-8"
                    )
                else:
                    context.routing_capture_warnings.append(
                        f"Could not capture logs for {service_namespace}/{pod_name}/{container_name}: "
                        f"{logs.stderr.strip()}"
                    )

    if not seen_pods:
        context.routing_capture_warnings.append(
            "No Gateway pods found through Gateway Service selectors"
        )
    return f"Captured {len(seen_pods)} Gateway pods"


@task
def record_routing_capture_warnings(args, context):
    """Make missing diagnostics visible without masking the test result."""
    warnings = context.routing_capture_warnings
    if warnings:
        for warning in warnings:
            logger.warning(warning)
        (args.artifact_dir / "artifacts" / "routing_capture_warnings.txt").write_text(
            "\n".join(warnings) + "\n", encoding="utf-8"
        )
    return f"{len(warnings)} routing capture warnings"
def _container_crash(pod_name: str, status: dict) -> str | None:
    """Describe how a container crashed, or None if it did not."""
    container_name = status.get("name", "<unknown>")

    waiting_reason = status.get("state", {}).get("waiting", {}).get("reason")
    if waiting_reason == "CrashLoopBackOff":
        return f"{pod_name}/{container_name}: waiting (CrashLoopBackOff)"

    for state_key in ("state", "lastState"):
        terminated = status.get(state_key, {}).get("terminated", {})
        exit_code = terminated.get("exitCode")
        if terminated and exit_code not in (None, 0):
            reason = terminated.get("reason", "Unknown")
            return f"{pod_name}/{container_name}: {state_key} terminated ({reason}, exit code {exit_code})"

    return None


def _find_crashed_containers(pods: list) -> list[str]:
    """Return a crash description per crashed container across the given pods (empty if none)."""
    crashes = []
    for pod in pods:
        pod_name = pod.get("metadata", {}).get("name", "<unknown>")
        for status_key in (
            "initContainerStatuses",
            "containerStatuses",
            "ephemeralContainerStatuses",
        ):
            for status in pod.get("status", {}).get(status_key, []):
                crash = _container_crash(pod_name, status)
                if crash:
                    crashes.append(crash)
    return crashes


@task
def check_pods_crashed(args, context):
    """Fail the capture if any LLMInferenceService pod container crashed.

    Runs after all capture tasks so the artifacts are still written on failure.
    """
    pods = oc_get_json(
        "pods",
        namespace=context.target_namespace,
        selector=f"app.kubernetes.io/name={args.llmisvc_name}",
    )
    crashed = _find_crashed_containers((pods or {}).get("items", []))
    if crashed:
        raise RuntimeError("Crashed LLMInferenceService containers detected: " + "; ".join(crashed))
    return "No crashed containers detected in LLMInferenceService pods"


if __name__ == "__main__":
    run.main()
