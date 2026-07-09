"""
Preflight checks for MCP Gateway performance tests.

Validates that the cluster is ready for testing after the prepare phase:
1. Required CRDs are installed
2. MCP Gateway deployment is running in mcp-system
3. MCPGatewayExtension CR exists and is reconciled
4. Test namespace exists
5. Deployed version matches the requested version
6. End-to-end connectivity: deploy a probe server, verify gateway
   recognition via MCPServerRegistration Ready condition, then clean up

Usage:
    python -m projects.mcp_gateway.orchestration.ci preflight
"""

from __future__ import annotations

import json
import logging
import os
import re

from projects.agentic_tools.mcp.toolbox.deploy_mock_servers import main as deploy_mock_servers
from projects.core.dsl.utils.k8s import oc, oc_get_json, oc_resource_exists
from projects.core.library import config
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.mcp_gateway.orchestration.runtime_config import cfg
from projects.mcp_gateway.toolbox.apply_infrastructure import main as apply_infra

logger = logging.getLogger(__name__)

_PREFLIGHT_NAME_PREFIX = "preflight-probe"


class PreflightError(RuntimeError):
    """A preflight check failed."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run() -> int:
    """Execute all preflight checks. Returns 0 on success, 1 on failure."""
    checks = [
        ("Required CRDs present", check_crds_present),
        ("MCP Gateway deployment running", check_gateway_deployment),
        ("MCPGatewayExtension reconciled", check_extension_reconciled),
        ("Test namespace exists", check_test_namespace),
        ("Deployed version matches requested", check_version_match),
        ("Gateway connectivity (probe)", check_gateway_connectivity),
    ]

    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    logger.info("=" * 60)
    logger.info("MCP Gateway Preflight Checks")
    logger.info("=" * 60)

    for name, check_fn in checks:
        logger.info("")
        logger.info("--- %s ---", name)
        try:
            check_fn()
            logger.info("  PASS: %s", name)
            passed.append(name)
        except PreflightError as exc:
            logger.error("  FAIL: %s: %s", name, exc)
            failed.append((name, str(exc)))
        except Exception as exc:
            logger.error("  FAIL: %s (unexpected): %s", name, exc)
            failed.append((name, f"unexpected error: {exc}"))

    logger.info("")
    logger.info("=" * 60)
    logger.info("Preflight summary: %d passed, %d failed", len(passed), len(failed))

    if failed:
        for name, msg in failed:
            logger.error("  FAIL  %s: %s", name, msg)
        return 1

    logger.info("All preflight checks passed")
    return 0


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_crds_present() -> None:
    """Verify that the MCP Gateway CRDs are installed on the cluster."""
    api_group = cfg.get_api_group()
    required_crds = [
        f"mcpserverregistrations.{api_group}",
        f"mcpgatewayextensions.{api_group}",
    ]

    missing = []
    for crd in required_crds:
        if not oc_resource_exists("crd", crd):
            missing.append(crd)

    if missing:
        raise PreflightError(f"Missing CRDs: {', '.join(missing)}")

    logger.info("  Found CRDs: %s", ", ".join(required_crds))


def check_gateway_deployment() -> None:
    """Verify that the mcp-gateway deployment exists and has ready replicas."""
    namespace = "mcp-system"
    deployment = oc_get_json(
        "deployment",
        name="mcp-gateway",
        namespace=namespace,
        ignore_not_found=True,
    )
    if not deployment:
        raise PreflightError(
            f"deployment/mcp-gateway not found in {namespace}. "
            "Did the prepare phase complete successfully?"
        )

    available = deployment.get("status", {}).get("availableReplicas", 0)
    if not available:
        raise PreflightError(
            f"deployment/mcp-gateway exists but has no ready replicas "
            f"(availableReplicas={available})"
        )

    logger.info("  deployment/mcp-gateway: %d replica(s) available", available)


def check_extension_reconciled() -> None:
    """Verify that an MCPGatewayExtension CR exists and has been reconciled."""
    api_group = cfg.get_api_group()
    result = oc(
        "get",
        f"mcpgatewayextensions.{api_group}",
        "-A",
        "-o",
        "json",
        check=False,
        log_stdout=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PreflightError("No MCPGatewayExtension resources found on the cluster")

    data = json.loads(result.stdout)
    items = data.get("items", [])
    if not items:
        raise PreflightError("No MCPGatewayExtension resources found on the cluster")

    ext = items[0]
    ext_name = ext["metadata"]["name"]
    ext_ns = ext["metadata"].get("namespace", "?")
    conditions = ext.get("status", {}).get("conditions", [])

    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    if not ready:
        condition_summary = "; ".join(f"{c.get('type')}={c.get('status')}" for c in conditions)
        raise PreflightError(
            f"MCPGatewayExtension {ext_ns}/{ext_name} is not Ready "
            f"(conditions: {condition_summary or 'none'})"
        )

    logger.info("  MCPGatewayExtension %s/%s is Ready", ext_ns, ext_name)


def check_test_namespace() -> None:
    """Verify that the test namespace exists (or can be created)."""
    namespace = cfg.get_namespace()
    if oc_resource_exists("namespace", namespace):
        logger.info("  Namespace %s exists", namespace)
    else:
        logger.info("  Namespace %s does not exist, creating it", namespace)
        ensure_namespace(
            namespace,
            labels={
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/project": "mcp_gateway",
            },
        )
        logger.info("  Namespace %s created", namespace)


def check_version_match() -> None:
    """Verify that the deployed MCP Gateway version matches what was requested."""
    requested = config.project.get_config(
        "infrastructure.mcp_gateway_version", None, print=False, warn=False
    ) or os.environ.get("MCP_GATEWAY_VERSION")

    if not requested:
        logger.info("  No explicit version requested, skipping version match check")
        return

    deployed = cfg.get_deployed_version()
    requested_normalized = _normalize_version(requested)
    deployed_normalized = _normalize_version(deployed)

    if requested_normalized != deployed_normalized:
        raise PreflightError(
            f"Version mismatch: requested '{requested}' (normalized: "
            f"'{requested_normalized}') but deployed '{deployed}' (normalized: "
            f"'{deployed_normalized}'). The prepare phase may have installed "
            f"the wrong version."
        )

    logger.info("  Version match: requested=%s, deployed=%s", requested, deployed)


def check_gateway_connectivity() -> None:
    """End-to-end connectivity probe.

    Deploys a single mock server into the test namespace using the existing
    deploy_mock_servers toolbox, creates gateway infrastructure (HTTPRoute +
    MCPServerRegistration) using the apply_infrastructure toolbox, waits for
    the registration to become Ready (proving the gateway recognized it),
    then tears everything down via the existing cleanup functions.
    """
    namespace = cfg.get_namespace()
    api_group = cfg.get_api_group()
    mock_image = _get_mock_server_image()

    logger.info("  Deploying probe server in %s ...", namespace)

    try:
        deploy_mock_servers.run(
            namespace=namespace,
            count=1,
            image=mock_image,
            name_prefix=_PREFLIGHT_NAME_PREFIX,
            tools_per_server=1,
            labels={"forge.openshift.io/purpose": "preflight"},
        )

        apply_infra.run(
            namespace=namespace,
            count=1,
            name_prefix=_PREFLIGHT_NAME_PREFIX,
            api_group=api_group,
        )

        logger.info("  Gateway recognized the probe server")
    except Exception as exc:
        raise PreflightError(f"Connectivity probe failed: {exc}") from exc
    finally:
        logger.info("  Cleaning up probe resources ...")
        _cleanup_probe(namespace=namespace, api_group=api_group)

    logger.info("  Connectivity probe passed")


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------


def _cleanup_probe(*, namespace: str, api_group: str) -> None:
    """Remove only the probe resources created by the connectivity check.

    Deletes by known resource names derived from _PREFLIGHT_NAME_PREFIX
    rather than the broad MOCK_MCP_LABEL selector, to avoid accidentally
    removing test-phase resources sharing the same label.
    """
    probe_name = f"{_PREFLIGHT_NAME_PREFIX}-1"

    for resource_type in ("deployment", "service"):
        oc(
            "delete",
            resource_type,
            probe_name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "--wait=false",
            check=False,
        )

    for resource_type in (f"mcpserverregistrations.{api_group}",):
        oc(
            "delete",
            resource_type,
            probe_name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "--wait=false",
            check=False,
        )

    oc(
        "delete",
        "httproute",
        f"{probe_name}-route",
        "-n",
        namespace,
        "--ignore-not-found=true",
        "--wait=false",
        check=False,
    )

    oc(
        "delete",
        "destinationrule",
        f"{probe_name}-mtls-disable",
        "-n",
        "istio-system",
        "--ignore-not-found=true",
        "--wait=false",
        check=False,
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _normalize_version(version: str) -> str:
    """Normalize a version string for comparison.

    Strips whitespace, leading ``v``, and the ``sha-`` prefix that ghcr.io
    image tags use (e.g. ``sha-7ce4c58…`` → ``7ce4c58…``).
    """
    v = version.strip().lstrip("v")
    v = re.sub(r"^sha-", "", v)
    match = re.match(r"(\d+\.\d+\.\d+)", v)
    return match.group(1) if match else v


def _get_mock_server_image() -> str:
    """Resolve the mock server container image from config."""
    try:
        mock_cfg = cfg.get_mock_server_config()
        return mock_cfg.get("image", "quay.io/rh-ee-aharush/perf-mock-server:latest")
    except Exception:
        return "quay.io/rh-ee-aharush/perf-mock-server:latest"
