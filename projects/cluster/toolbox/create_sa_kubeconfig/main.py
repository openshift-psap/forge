#!/usr/bin/env python3

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import yaml

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_get_json, oc_resource_exists

logger = logging.getLogger("DSL")


@entrypoint
def run(
    sa_name: str,
    *,
    namespace: str = "",
    kubeconfig_file: str,
    create_token_secret: bool = True,
    token_duration: str = "8760h",
) -> int:
    """
    Generate kubeconfig file from a Service Account using non-persisting credential delivery.

    This tool generates a working kubeconfig file that can be used to authenticate as a specific
    Service Account. It supports both long-lived token secrets and temporary tokens.

    Args:
        sa_name: Name of the Service Account
        namespace: Namespace where the Service Account exists/will be created (default: current project)
        kubeconfig_file: Output file path for working kubeconfig
        create_token_secret: Create a long-lived token secret instead of using kubectl create token
        token_duration: Duration for temporary tokens when create_token_secret=False (default: 8760h)
    """

    execute_tasks(locals())
    return 0


@task
def setup_configuration(args, ctx):
    """Setup configuration and validate parameters"""

    # Set default namespace from current project if not provided
    if args.namespace:
        ctx.namespace = args.namespace
    else:
        result = oc("project", "-q")
        ctx.namespace = result.stdout.strip()
        if not ctx.namespace:
            raise ValueError("Couldn't find the current namespace")

    return f"Prepared to create kubeconfig for SA {args.sa_name} in namespace {ctx.namespace}"


@task
def validate_parameters(args, ctx):
    """Validate command parameters"""

    if not args.sa_name:
        raise ValueError("sa_name cannot be empty")

    # Validate token duration format for temporary tokens
    if not args.create_token_secret:
        if not args.token_duration.endswith(("s", "m", "h")):
            raise ValueError(
                "token_duration must end with 's', 'm', or 'h' (e.g., '1h', '30m', '3600s')"
            )

    return f"Validated parameters for Service Account {args.sa_name}"


@task
def ensure_service_account(args, ctx):
    """Create Service Account if it doesn't exist"""

    if oc_resource_exists("serviceaccount", args.sa_name, namespace=ctx.namespace):
        return f"Service Account {args.sa_name} already exists in namespace {ctx.namespace}"

    oc(
        "create",
        "serviceaccount",
        args.sa_name,
        "-n",
        ctx.namespace,
    )

    return f"Created Service Account {args.sa_name} in namespace {ctx.namespace}"


@task
def create_token_secret(args, ctx):
    """Create a token secret for the Service Account if requested"""

    if not args.create_token_secret:
        return "Using temporary token approach, skipping token secret creation"

    ctx.secret_name = f"{args.sa_name}-token"

    # Check if secret already exists
    if oc_resource_exists("secret", ctx.secret_name, namespace=ctx.namespace):
        return f"Token secret {ctx.secret_name} already exists in namespace {ctx.namespace}"

    # Create token secret manifest
    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": ctx.secret_name,
            "namespace": ctx.namespace,
            "annotations": {"kubernetes.io/service-account.name": args.sa_name},
        },
        "type": "kubernetes.io/service-account-token",
    }

    # Serialize manifest to YAML and apply through stdin
    secret_yaml = yaml.dump(secret_manifest, default_flow_style=False)
    oc("apply", "-f", "-", input_text=secret_yaml, handled_secretly=True)

    return f"Created token secret {ctx.secret_name}"


@retry(attempts=30, delay=2, backoff=1.0)
@task
def wait_for_token_secret_ready(args, ctx):
    """Wait for token secret to be populated"""

    if not args.create_token_secret:
        return "Using temporary token approach, no secret to wait for"

    try:
        secret_data = oc_get_json(
            "secret", name=ctx.secret_name, namespace=ctx.namespace, handled_secretly=True
        )
        if not secret_data:
            return False, f"Secret {ctx.secret_name} not found"

        data = secret_data.get("data", {})
        if not data.get("token") or not data.get("ca.crt"):
            return False, f"Secret {ctx.secret_name} not fully populated yet"

        return f"Token secret {ctx.secret_name} is ready"

    except Exception as e:
        return False, f"Error checking secret {ctx.secret_name}: {e}"


@task
def extract_cluster_info(args, ctx):
    """Extract cluster API server URL"""

    # Get API server URL
    result = oc("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    ctx.api_server = result.stdout.strip()

    if not ctx.api_server:
        raise RuntimeError("Could not extract API server URL from current kubeconfig")

    return f"Extracted cluster info: API server {ctx.api_server}"


def _build_cluster_config(ctx):
    """Build cluster configuration section for kubeconfig"""
    return {
        "server": ctx.api_server,
        "insecure-skip-tls-verify": True,
    }


@task
def generate_kubeconfig(args, ctx):
    """Generate working kubeconfig file using non-persisting credential delivery"""

    # Extract token locally (not stored in context)
    if args.create_token_secret:
        # Extract token from secret (with secret handling flag to prevent logging)
        secret_data = oc_get_json(
            "secret", name=ctx.secret_name, namespace=ctx.namespace, handled_secretly=True
        )
        if not secret_data:
            raise RuntimeError(f"Token secret {ctx.secret_name} not found")

        token_b64 = secret_data.get("data", {}).get("token")
        if not token_b64:
            raise RuntimeError(f"Token not found in secret {ctx.secret_name}")

        token = base64.b64decode(token_b64).decode("utf-8")

        # Validate token format and length for security
        if len(token) > 10000:  # Prevent DoS with excessively long tokens
            raise RuntimeError("Token is unexpectedly long - potential security issue")

    else:
        # Create temporary token
        result = oc(
            "create",
            "token",
            args.sa_name,
            "-n",
            ctx.namespace,
            f"--duration={args.token_duration}",
            handled_secretly=True,
        )

        token = result.stdout.strip()
        if not token:
            raise RuntimeError("Failed to create temporary token")

        # Validate token format and length for security
        if len(token) > 10000:  # Prevent DoS with excessively long tokens
            raise RuntimeError("Token is unexpectedly long - potential security issue")

    # Create working kubeconfig with token
    working_kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "cluster": _build_cluster_config(ctx),
                "name": "cluster",
            }
        ],
        "contexts": [
            {
                "context": {
                    "cluster": "cluster",
                    "namespace": ctx.namespace,
                    "user": args.sa_name,
                },
                "name": args.sa_name,
            }
        ],
        "current-context": args.sa_name,
        "users": [{"name": args.sa_name, "user": {"token": token}}],  # Working token included
    }

    # Write working kubeconfig to specified file path with secure permissions
    kubeconfig_path = Path(args.kubeconfig_file)

    with open(kubeconfig_path, "w") as f:
        yaml.dump(working_kubeconfig, f, default_flow_style=False)

    # Ensure permissions are correctly set after writing
    os.chmod(kubeconfig_path, 0o600)

    # Store path for reference (no tokens or structure stored in context)
    ctx.kubeconfig_path = str(kubeconfig_path)

    return f"Generated working kubeconfig: {ctx.kubeconfig_path}"


@task
def validate_kubeconfig(args, ctx):
    """Validate that the generated kubeconfig works"""

    # Test the kubeconfig by running a simple command

    result = oc(
        "--kubeconfig",
        ctx.kubeconfig_path,
        "whoami",
        check=True,
    )

    if result.returncode == 0 and result.stdout.strip():
        username = result.stdout.strip()
        return f"Kubeconfig validation successful - authenticated as: {username}"
    else:
        raise RuntimeError("Authentication test returned no username")


@task
def display_usage_instructions(args, ctx):
    """Display usage instructions"""

    instructions = f"""
# Working kubeconfig created successfully!

## Usage:
# Use the kubeconfig file directly
export KUBECONFIG={ctx.kubeconfig_path}
oc get pods

# Or specify the kubeconfig file explicitly
oc --kubeconfig={ctx.kubeconfig_path} get pods

## Files created:
- {ctx.kubeconfig_path} (working kubeconfig file with authentication)

## Security Note:
- Kubeconfig file contains authentication token for the Service Account
- File has restrictive permissions (0600) - only readable by owner
- Uses non-persisting credential delivery (token not stored in execution context)
"""

    if args.create_token_secret:
        instructions += f"\n## Token source: Long-lived secret {ctx.secret_name}"
    else:
        instructions += f"\n## Token source: Temporary token (expires in {args.token_duration})"

    print(instructions)

    return f"Working kubeconfig ready: {ctx.kubeconfig_path}"


if __name__ == "__main__":
    run.main()
