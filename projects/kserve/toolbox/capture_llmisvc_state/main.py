#!/usr/bin/env python3

"""
LLMInferenceService state capture using task-based DSL
Replaces llmd_capture_llmisvc_state Ansible role
"""

from projects.core.dsl import entrypoint, execute_tasks, shell, task


@entrypoint
def run(llmisvc_name: str, *, namespace: str = ""):
    """
    Capture LLMInferenceService state using task-based DSL

    Args:
        llmisvc_name: Name of the LLMInferenceService to capture
        namespace: Namespace of the LLMInferenceService (empty string auto-detects current namespace)
    """

    return execute_tasks(locals())


@task
def setup_directories(args, context):
    """Create the artifacts directory"""

    shell.mkdir("artifacts")
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
        stdout_dest=args.artifact_dir / "artifacts/namespace.pods.status",
        check=False,
    )
    return "Namespace pods status captured"


@task
def capture_namespace_services(args, context):
    """Capture all services in the namespace"""
    shell.run(
        f"oc get svc -n {context.target_namespace}",
        stdout_dest=args.artifact_dir / "artifacts/namespace.services.status",
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
def capture_pod_logs(args, context):
    """Capture logs from LLMInferenceService pods"""
    result = shell.run(
        f'oc get pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
        log_stdout=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return "No pods found to capture logs"

    log_file = args.artifact_dir / "artifacts/llminferenceservice.pods.logs"

    with open(log_file, "w") as handle:
        for pod_name in pod_names:
            handle.write(f"=== Logs for pod: {pod_name} ===\n")
            log_result = shell.run(
                f"oc logs {pod_name} -n {context.target_namespace} --all-containers=true",
                check=False,
                log_stdout=False,
            )
            handle.write(log_result.stdout)
            handle.write("\n")

    return f"Pod logs captured for {len(pod_names)} pods"


@task
def capture_pod_previous_logs(args, context):
    """Capture previous logs from LLMInferenceService pods if available"""
    result = shell.run(
        f'oc get pods -l "app.kubernetes.io/name={args.llmisvc_name}" -n {context.target_namespace} -o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return "No pods found to capture previous logs"

    log_file = args.artifact_dir / "artifacts/llminferenceservice.pods.previous.logs"

    with open(log_file, "w") as handle:
        for pod_name in pod_names:
            handle.write(f"=== Previous logs for pod: {pod_name} ===\n")
            log_result = shell.run(
                f"oc logs {pod_name} -n {context.target_namespace} --previous --all-containers=true",
                check=False,
                log_stdout=False,
            )
            handle.write(log_result.stdout)
            handle.write("\n")

    return f"Pod previous logs captured for {len(pod_names)} pods"


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


if __name__ == "__main__":
    run.main()
