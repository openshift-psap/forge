# FORGE Toolbox Command Writing Guide

**Version**: 1.0
**Target Audience**: Developers, AI Agents
**Last Updated**: 2026-05-06

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Design Principles](#design-principles)
3. [Command Structure](#command-structure)
4. [DSL Usage Guide](#dsl-usage-guide)
5. [Input/Output Guidelines](#inputoutput-guidelines)
6. [Artifact Management](#artifact-management)
7. [Security Best Practices](#security-best-practices)
8. [Templating and Kubernetes Objects](#templating-and-kubernetes-objects)
9. [Troubleshooting and Post-Mortem Support](#troubleshooting-and-post-mortem-support)
10. [Examples](#examples)
11. [Common Antipatterns](#common-antipatterns)

---

## Architecture Overview

FORGE follows a **three-layer architecture** inspired by TOPSAIL but using a Python DSL instead of Ansible:

```text
┌─────────────────────┐
│   ORCHESTRATION     │  ← CI entrypoints, config-driven workflows
│   (pre_cleanup,     │    Uses config files to coordinate testing
│    prepare, test,   │
│    post_cleanup)    │
├─────────────────────┤
│     TOOLBOX         │  ← Focused, standalone commands
│   (deploy operator, │    One task = one command
│    scale cluster,   │    CLI reproducible, no external deps
│    deploy service)  │
├─────────────────────┤
│  POST-PROCESSING    │  ← Visualization, KPIs, regression analysis
│   (parsing, plots,  │    Report generation and historical tracking
│    reporting, KPIs) │
└─────────────────────┘
```

**Key Insight**: The toolbox layer is **NOT plain Python**. It's a Python **DSL** (Domain Specific Language) that enforces structure, logging, and troubleshooting capabilities.

---

## Design Principles

### 1. Single Responsibility
- **One toolbox command = one high-level task**
- Think cookbook/algorithm: "install operator", "scale cluster", "create LLMISVC", "wait for pod"
- If you find yourself doing multiple unrelated things, split into separate commands

### 2. Standalone and Reproducible
- **No external dependencies** outside the command directory (except global platform conventions)
- **CLI reproducible**: you should be able to copy-paste the command from logs and run it manually
- **Simple parameters**: flat scalars, avoid complex nested structures unless absolutely necessary

### 3. Easy Troubleshooting
- **One directory per command execution** in logs - you can see at a glance which steps succeeded/failed
- **Clear task names** that explain what's happening
- **Comprehensive artifact capture** for post-mortem analysis and scrutiny

### 4. Reusability Across Projects
- **Scalar parameters instead of single config blob**
- Libraries for common rendering functions (like K8s object generation)
- Think about what other projects will need

---

## Command Structure

### Directory Layout Example
```text
projects/your_project/toolbox/your_command/
├── main.py              # Entry point with @entrypoint
├── templates/           # Jinja2 templates for K8s objects (optional)
│   ├── pod.yaml.j2
│   └── service.yaml.j2
└── scripts/             # Static scripts (optional)
    └── setup.sh
```

### Entry Point Pattern
```python
@entrypoint
def run(
    # Required positional parameters
    cluster_name: str,
    namespace: str,
    # Optional keyword parameters with defaults
    *,
    replicas: int = 1,
    timeout_minutes: int = 30,
    gpu_type: str = None,
):
    """
    Brief description of what this command does

    Args:
        cluster_name: Description of this parameter
        namespace: Kubernetes namespace to use
        replicas: Number of replicas to deploy (default: 1)
        timeout_minutes: How long to wait for completion (default: 30)
        gpu_type: GPU type required, if any (default: None)
    """
    return execute_tasks(locals())
```

---

## DSL Usage Guide

### Task Definition
```python
@task
def your_task_name(args, ctx):
    """Clear description of what this task does"""

    # Access parameters
    cluster_name = args.cluster_name
    namespace = args.namespace
    artifact_dir = args.artifact_dir

    # Use context for data sharing between tasks
    ctx.some_value = "computed result"

    # Execute commands
    shell.run("oc get pods")

    # Return success message or truthy value
    return "Task completed successfully"
```

### Task Decorators

#### Conditional Execution
```python
@when(lambda: previous_task.status.return_value is True)
@task
def conditional_task(args, ctx):
    """Only run if previous_task succeeded"""
    return "Only ran because condition was met"
```

#### Retry Logic
```python
@retry(attempts=5, delay=10, backoff=1.0)
@task
def wait_for_resource(args, ctx):
    """Wait for a resource to be ready"""

    result = shell.run("oc get pod mypod -o jsonpath='{.status.phase}'", check=False)

    if not result.success:
        return False  # Retry

    if result.stdout.strip() == "Running":
        return "Pod is ready"
    else:
        return False  # Retry
```

#### Always Execute (Cleanup and K8s Status Capture)
```python
@always
@task
def capture_resources(args, ctx):
    """Clean up resources even if previous tasks failed"""

    # This always runs, even after failures
    shell.run("oc delete pod mypod", check=False)
    return "Cleanup completed"


@always
@task
def cleanup_resources(args, ctx):
    """Clean up resources even if previous tasks failed"""

    # This always runs, even after failures
    shell.run("oc delete pod mypod", check=False)
    return "Cleanup completed"
```

### Command Execution
```python
from projects.core.dsl import shell

# Basic command
shell.run("oc get pods")

# With output capture
result = shell.run("oc get pods -o json")
if result.success:
    import json

    pods = json.loads(result.stdout)

# Save output to file
shell.run(
    "oc describe pod mypod", stdout_dest=args.artifact_dir / "artifacts" / "pod-description.txt"
)

# Don't fail on error
shell.run("oc delete pod optional-pod", check=False)
```

---

## Input/Output Guidelines

### ✅ GOOD: Scalar Parameters
```python
def run(
    cluster_name: str,
    namespace: str,
    benchmark: str,
    *,
    platform: str = "aws",
    replicas: int = 1,
):
```

### ❌ BAD: Config Blob
```python
def run(inputs_file: str):  # Don't do this!
    with open(inputs_file) as f:
        config = yaml.load(f)
    # Now you have to guess what's in config...
```

### File Parameters
Files are acceptable for:
- **Secrets** (mandatory for security)
- **Complex YAML manifests** that shouldn't be parameters
- **Large data** that doesn't fit in command line

```python
def run(
    secret_file: str,           # For passwords, tokens
    manifest_file: str = None,  # For complex K8s YAML
):
```

### Environment Variables
- **KUBECONFIG** is OK (platform convention)
- **NO other environment variables** should be needed
- **NO passwords in environment variables** (they appear in logs!)

---

## Artifact Management

### Directory Structure
```text
{artifact_dir}/
├── artifacts/           # Data FROM the cluster (oc get, oc describe)
│   ├── pod-status.yaml
│   ├── service-describe.txt
│   └── logs/
│       └── mypod.log
└── src/                 # Data TO the cluster (oc create, oc apply)
    ├── pod-manifest.yaml
    └── service-manifest.yaml
```

### Usage Guidelines

#### Artifacts Directory (`artifacts/`)
- **Capture cluster state**: `oc get`, `oc describe`, `oc logs`
- **For human review**: prefer YAML format
- **For large datasets**: capture both JSON (fast parsing) and YAML (human readable)
- **For post-processing**: capture JSON

```python
@task
def capture_pod_status(args, ctx):
    """Capture pod information for debugging"""

    # Human readable
    shell.run(
        "oc describe pod mypod", stdout_dest=args.artifact_dir / "artifacts" / "pod-describe.txt"
    )

    # Machine readable
    shell.run(
        "oc get pod mypod -o yaml", stdout_dest=args.artifact_dir / "artifacts" / "pod-status.yaml"
    )

    # For large scale tests, also capture JSON
    shell.run("oc get pods -o json", stdout_dest=args.artifact_dir / "artifacts" / "all-pods.json")
```

#### Source Directory (`src/`)
- **Generated manifests**: anything you `oc apply`
- **Configuration files**: derived from templates

```python
@task
def create_pod_manifest(args, ctx):
    """Generate pod manifest from template"""

    manifest_file = args.artifact_dir / "src" / "pod-manifest.yaml"
    shell.mkdir(manifest_file.parent)

    template.render_template_to_file("pod.yaml.j2", manifest_file)

    shell.run(f"oc apply -f {manifest_file}")

    return f"Applied manifest: {manifest_file}"
```

---

## Security Best Practices

### Secret Handling

#### ✅ GOOD: File-based secrets
```python
@task
def create_secret(args, ctx):
    """Create secret from file"""

    # Create secret directly from file (avoids shell interpolation)
    shell.run(["oc", "create", "secret", "generic", "mysecret", "--from-file", args.secret_file])

    return "Secret created"
```


#### ✅ GOOD: No logging for sensitive operations  
```python
@task
def handle_sensitive_data(args, ctx):
    """Process sensitive information"""

    # Use log_command=False to prevent parameter logging
    with open(args.secret_file) as f:
        secret_data = f.read()

    # Process secret_data...
    shell.run("oc apply -f -", stdin=secret_data, log_command=False)
    # Sensitive command content is not logged
```

#### ❌ BAD: Secrets in parameters
```python
def run(password: str):  # DON'T DO THIS!
    # password will appear in logs!
```

#### ❌ BAD: Secrets in environment variables
```python
os.environ["SECRET_TOKEN"] = token  # DON'T DO THIS!
# Environment variables appear in logs and process lists
```

### Secure Command Execution
```python
# For commands that might contain secrets
shell.run("oc apply -f -", stdin=secret_yaml_content, log_command=False)
```

---

## Templating and Kubernetes Objects

### ✅ GOOD: Use Templates
Store K8s objects as Jinja2 templates, not inline Python:

```yaml
# templates/pod.yaml.j2
apiVersion: v1
kind: Pod
metadata:
  name: {{ args.pod_name }}
  namespace: {{ args.namespace }}
  labels:
    app: {{ args.app_name }}
spec:
  containers:
  - name: main
    image: {{ args.image }}
    resources:
      requests:
        cpu: {{ args.cpu }}
        memory: {{ args.memory }}
```

```python
@task
def create_pod(args, ctx):
    """Create pod from template"""

    pod_file = args.artifact_dir / "src" / f"{args.pod_name}-pod.yaml"
    template.render_template_to_file("pod.yaml.j2", pod_file)

    shell.run(f"oc apply -f {pod_file}")
    return f"Pod {args.pod_name} created"
```

### Static Scripts with Environment Variables
Scripts should be static files that use environment variables (set in pod specs):

```bash
# scripts/setup.sh (static file - no template!)
#!/bin/bash
set -euo pipefail

echo "Setting up ${SERVICE_NAME}..."
echo "Namespace: ${NAMESPACE}"
echo "Replicas: ${REPLICAS}"

# Your script logic using environment variables
curl -X POST "${API_ENDPOINT}/setup" \
  -H "Content-Type: application/json" \
  -d "{\"service\": \"${SERVICE_NAME}\", \"replicas\": ${REPLICAS}}"
```

Environment variables are defined in the pod template:
```yaml
# templates/pod.yaml.j2
apiVersion: v1
kind: Pod
metadata:
  name: {{ args.pod_name }}
spec:
  containers:
  - name: setup
    image: {{ args.image }}
    env:
    - name: SERVICE_NAME
      value: "{{ args.service_name }}"
    - name: NAMESPACE
      value: "{{ args.namespace }}"
    - name: REPLICAS
      value: "{{ args.replicas }}"
    - name: API_ENDPOINT
      value: "{{ args.api_endpoint }}"
    command: ["/scripts/setup.sh"]
```

```python
@task
def create_setup_configmap(args, ctx):
    """Create ConfigMap with static script"""

    # Copy static script to src for reference
    script_source = Path(__file__).parent / "scripts" / "setup.sh"
    script_dest = args.artifact_dir / "src" / "setup.sh"
    shell.run(f"cp {script_source} {script_dest}")

    # Create ConfigMap with the script
    shell.run(f"oc create configmap setup-script --from-file={script_source}")

    return "Setup script ConfigMap created"
```

### ❌ BAD: Inline K8s Objects
```python
# Don't do this!
@task
def create_pod_inline(args, ctx):
    pod_yaml = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {args.pod_name}
...
    """
    # IDEs can't syntax highlight this properly
    # Hard to maintain and read
```

---

## Troubleshooting and Post-Mortem Support

### Task Naming Strategy
Use descriptive task names that explain the **purpose**, not just the action:

#### ✅ GOOD
```python
@task
def wait_for_operator_to_be_ready(args, ctx):
    """Wait for operator deployment to reach ready state"""


@task
def capture_failed_pod_logs(args, ctx):
    """Collect logs from pods that failed to start"""


@task
def verify_service_endpoints_are_available(args, ctx):
    """Check that service endpoints are responding to requests"""
```

#### ❌ BAD
```python
@task
def check_stuff(args, ctx):  # Too vague

@task
def run_commands(args, ctx):  # Too generic
```

### Error Context
When tasks fail, provide context for debugging:

```python
@task
def wait_for_resource_ready(args, ctx):
    """Wait for custom resource to be ready"""

    result = shell.run(f"oc get myresource {args.resource_name} -o jsonpath='{{.status.ready}}'")

    if not result.success:
        # Capture debugging info before failing
        shell.run(
            f"oc describe myresource {args.resource_name}",
            stdout_dest=args.artifact_dir / "artifacts" / "failed-resource-describe.txt",
            check=False,
        )
        raise RuntimeError(f"Failed to query resource {args.resource_name}: {result.stderr}")

    if result.stdout.strip() != "true":
        return False  # Retry

    return f"Resource {args.resource_name} is ready"
```

### Always Tasks for Cleanup and State Capture
```python
@always
@task
def capture_cluster_state(args, ctx):
    """Capture cluster state for post-mortem analysis"""

    # Capture even if tests failed
    shell.run(
        "oc get pods --all-namespaces",
        stdout_dest=args.artifact_dir / "artifacts" / "all-pods.txt",
        check=False,
    )

    shell.run(
        "oc get events --sort-by='.lastTimestamp'",
        stdout_dest=args.artifact_dir / "artifacts" / "events.txt",
        check=False,
    )

    return "Cluster state captured"
```

---

## Examples

### Simple Example: Deploy a Service
```python
#!/usr/bin/env python3

from projects.core.dsl import always, entrypoint, execute_tasks, retry, shell, task, template


@entrypoint
def run(
    service_name: str,
    namespace: str,
    *,
    image: str = "nginx:latest",
    replicas: int = 1,
    port: int = 80,
):
    """
    Deploy a simple service to Kubernetes

    Args:
        service_name: Name of the service to create
        namespace: Target namespace
        image: Container image to use (default: nginx:latest)
        replicas: Number of replicas (default: 1)
        port: Service port (default: 80)
    """
    return execute_tasks(locals())


@task
def validate_inputs(args, ctx):
    """Validate input parameters"""

    if not args.service_name:
        raise ValueError("service_name is required")
    if not args.namespace:
        raise ValueError("namespace is required")
    if args.replicas < 1:
        raise ValueError("replicas must be >= 1")

    return "Inputs validated"


@task
def setup_directories(args, ctx):
    """Create artifact directories"""

    shell.mkdir("artifacts")
    shell.mkdir("src")
    return "Directories created"


@task
def verify_namespace_exists(args, ctx):
    """Ensure target namespace exists"""

    result = shell.run(f"oc get namespace {args.namespace}", check=False)
    if not result.success:
        raise RuntimeError(f"Namespace {args.namespace} does not exist")

    return f"Namespace {args.namespace} verified"


@task
def create_deployment_manifest(args, ctx):
    """Generate deployment manifest"""

    manifest_file = args.artifact_dir / "src" / f"{args.service_name}-deployment.yaml"
    template.render_template_to_file("deployment.yaml.j2", manifest_file)

    return f"Deployment manifest created: {manifest_file}"


@task
def apply_deployment(args, ctx):
    """Apply the deployment to cluster"""

    manifest_file = args.artifact_dir / "src" / f"{args.service_name}-deployment.yaml"
    shell.run(f"oc apply -f {manifest_file}")

    return f"Deployment {args.service_name} applied"


@retry(attempts=10, delay=5)
@task
def wait_for_deployment_ready(args, ctx):
    """Wait for deployment to be ready"""

    result = shell.run(
        f"oc get deployment {args.service_name} -n {args.namespace} "
        f"-o jsonpath='{{.status.readyReplicas}}'",
        check=False,
    )

    if not result.success:
        return False  # Retry

    ready_replicas = result.stdout.strip()
    if ready_replicas == str(args.replicas):
        return f"Deployment {args.service_name} is ready ({ready_replicas}/{args.replicas})"

    return False  # Retry


@always
@task
def capture_deployment_status(args, ctx):
    """Capture final deployment status"""

    # Capture deployment details
    shell.run(
        f"oc describe deployment {args.service_name} -n {args.namespace}",
        stdout_dest=args.artifact_dir / "artifacts" / "deployment-describe.txt",
        check=False,
    )

    # Capture pod status
    shell.run(
        f"oc get pods -l app={args.service_name} -n {args.namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "pods-status.yaml",
        check=False,
    )

    return "Deployment status captured"


if __name__ == "__main__":
    run.main()
```

---

## Common Antipatterns

### ❌ DON'T: Long, Complex Commands
```python
# BAD: Too many responsibilities in one command
@entrypoint
def run(inputs_file: str):
    # This command:
    # - Installs 3 different operators
    # - Scales the cluster
    # - Deploys 5 services
    # - Runs performance tests
    # - Generates reports

    # Split this into separate commands!
```

**Fix**: Split into focused commands:
- `install_operators`
- `scale_cluster`
- `deploy_services`
- `run_performance_tests`
- `generate_reports`

### ❌ DON'T: Build K8s Objects in Python
```python
# BAD: Hard to read and maintain
@task
def create_complex_deployment(args, ctx):
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": args.name},
        # 50 more lines of nested dictionaries...
    }
```

**Fix**: Use templates in `templates/deployment.yaml.j2`

### ❌ DON'T: Generic Task Names
```python
# BAD: What does this actually do?
@task
def step1(args, ctx):
@task
def do_stuff(args, ctx):
@task
def handle_things(args, ctx):
```

**Fix**: Descriptive names that explain the purpose

### ❌ DON'T: Secret Leakage
```python
# BAD: Secrets in parameters or environment
def run(password: str, api_key: str):

# BAD: Secrets in command lines
shell.run(f"curl -H 'Authorization: Bearer {token}' https://api.example.com")
```

**Fix**: Use file-based secrets with `log_command=False`

---

## Quick Checklist

When writing a toolbox command, ensure:

- [ ] **Single responsibility**: Command does one focused task
- [ ] **Scalar parameters**: No complex config blobs as input
- [ ] **CLI reproducible**: Can copy command from logs and run manually
- [ ] **Descriptive task names**: Clear what each task does
- [ ] **Templates for K8s objects**: Not inline Python dictionaries
- [ ] **Proper artifact organization**: `src/` for generated, `artifacts/` for captured
- [ ] **Security**: Secrets via files, never in parameters/environment
- [ ] **Always tasks**: Capture debugging info even on failure
- [ ] **Error context**: Helpful error messages with debugging info
- [ ] **Library functions**: Extract reusable rendering functions

---

## Getting Help

- **Specs**: Check `specs/008-toolbox-dsl/` for detailed DSL documentation
- **Examples**: Look at existing commands in `projects/*/toolbox/`
- **Code Review**: Have colleagues review for adherence to these principles

Remember: The toolbox is a **DSL**, not plain Python. Embrace the constraints - they make your commands more reliable, debuggable, and reusable!
