#!/usr/bin/env python3

from __future__ import annotations

import json
import logging

from projects.core.dsl import always, entrypoint, execute_tasks, retry, shell, task, template

logger = logging.getLogger("DSL")


@entrypoint
def run(
    catalog_source_name: str,
    catalog_namespace: str,
    catalog_image: str,
    *,
    display_name: str = "",
    publisher: str = "",
) -> int:
    """
    Deploy a CatalogSource from a custom index image and wait for it to become READY.

    Args:
        catalog_source_name: Name of the CatalogSource
        catalog_namespace: Namespace where the CatalogSource will be deployed
        catalog_image: Index image backing the CatalogSource
        display_name: Optional human-friendly label for logs
        publisher: Optional publisher label for the CatalogSource
    """

    execute_tasks(locals())
    return 0


@task
def setup_directories(args, ctx):
    """Create command source and artifact directories"""

    shell.mkdir("src")
    shell.mkdir("artifacts")

    return "Prepared src and artifacts directories"


@task
def validate_parameters(args, ctx):
    """Validate command parameters"""

    if not args.catalog_source_name:
        raise ValueError("catalog_source_name is required")
    if not args.catalog_namespace:
        raise ValueError("catalog_namespace is required")
    if not args.catalog_image:
        raise ValueError("catalog_image is required")

    return f"Validated custom catalog parameters for {args.display_name}"


@task
def ensure_namespace(args, ctx):
    """Create the catalog namespace if it does not exist."""
    shell.run(
        f"oc create namespace {args.catalog_namespace} --dry-run=client -o yaml | oc apply -f -",
    )
    return f"Ensured namespace {args.catalog_namespace}"


@task
def render_catalog_source_manifest(args, ctx):
    """Render the CatalogSource manifest"""

    ctx.catalog_manifest_file = (
        args.artifact_dir / "src" / f"{args.catalog_source_name}-catalogsource.yaml"
    )
    template.render_template_to_file("catalogsource.yaml.j2", ctx.catalog_manifest_file)
    return f"Rendered CatalogSource manifest for {args.display_name}"


@task
def apply_catalog_source(args, ctx):
    """Apply the CatalogSource manifest"""

    shell.run(f"oc apply -f {ctx.catalog_manifest_file}")
    return f"Applied CatalogSource {args.catalog_source_name}"


@retry(attempts=24, delay=5)  # 2 minutes total (24 * 5s)
@task
def wait_for_catalog_source_pod_running(args, ctx):
    """Wait for the CatalogSource pod to start running"""

    # Search for pod with the catalog source label
    result = shell.run(
        f"oc get pods -n {args.catalog_namespace} -l olm.catalogSource={args.catalog_source_name} --no-headers",
        check=False,
    )
    if not result.success:
        return False

    # Check if we have actual pods (not "No resources found")
    if "No resources found" in result.stdout:
        logger.info(f"No pods found yet for catalogSource {args.catalog_source_name}")
        return False

    # Count pods and check for multiple matches
    pod_count = len(result.stdout.strip().splitlines())
    if pod_count != 1:
        logger.warning(
            f"Multiple pods ({pod_count}) found for catalogSource {args.catalog_source_name}"
        )

    # Check for image issues
    if "ErrImagePull" in result.stdout:
        raise RuntimeError(f"CatalogSource pod image pull error: {result.stdout.strip()}")

    if "ImagePullBackOff" in result.stdout:
        raise RuntimeError(f"CatalogSource pod image pull back off: {result.stdout.strip()}")

    # Check for failed pods first - any failure should cause immediate error
    if "Failed" in result.stdout:
        raise RuntimeError(f"CatalogSource pod failed to start: {result.stdout.strip()}")

    # Look for Running status in the output
    if "Running" in result.stdout:
        return "CatalogSource pod is running"

    # Pods exist but not running yet
    return False


@retry(attempts=60, delay=15)
@task
def wait_for_catalog_source_ready(args, ctx):
    """Wait for the CatalogSource to report READY"""

    result = shell.run(
        f"oc get catalogsource {args.catalog_source_name} -n {args.catalog_namespace} -o json",
        check=False,
        log_stdout=False,
    )
    if not result.success:
        return False

    payload = json.loads(result.stdout)
    ctx.catalog_payload = payload
    state = payload.get("status", {}).get("connectionState", {}).get("lastObservedState", "")
    if state == "READY":
        return f"CatalogSource {args.catalog_source_name} is READY"
    return False


@always
@task
def capture_catalog_source(args, ctx):
    """Capture CatalogSource YAML and JSON"""

    shell.run(
        f"oc get catalogsource {args.catalog_source_name} -n {args.catalog_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "catalogsource.yaml",
        check=False,
    )
    shell.run(
        f"oc get catalogsource {args.catalog_source_name} -n {args.catalog_namespace} -o json",
        stdout_dest=args.artifact_dir / "artifacts" / "catalogsource.json",
        check=False,
    )
    return "Captured CatalogSource state"


@always
@task
def capture_namespace_pods(args, ctx):
    """Capture pods from the catalog namespace"""

    shell.run(
        f"oc get pods -n {args.catalog_namespace} -o wide",
        stdout_dest=args.artifact_dir / "artifacts" / "pods.status",
        check=False,
    )
    shell.run(
        f"oc get pods -n {args.catalog_namespace} -o yaml",
        stdout_dest=args.artifact_dir / "artifacts" / "pods.yaml",
        check=False,
    )
    return "Captured catalog namespace pods"


if __name__ == "__main__":
    run.main()
