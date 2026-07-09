#!/usr/bin/env python3
"""
Install the full MCP Gateway platform stack on an OpenShift cluster:
1. Service Mesh operator (kustomize) + wait for CRDs + instance (Istio, IstioCNI)
2. Connectivity Link operator (kustomize) + wait for CRDs
3. Kuadrant CR (Connectivity Link instance) + wait for authorino/limitador
4. MCP Gateway Helm install (controller + Gateway + ReferenceGrant, NO extension yet)
5. Wait for Gateway to be Programmed
6. Create MCPGatewayExtension CR (only after Gateway is ready)
7. Wait for broker deployment

Steps 4-6 are split to avoid a race condition: the controller deletes the
MCPGatewayExtension CR if it reconciles before the Gateway is programmed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from projects.cluster.toolbox.wait_for_crds import main as wait_for_crds_command
from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_apply, oc_get_json
from projects.core.library import env
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.mcp_gateway.toolbox.platform_helpers import (
    find_step,
    wait_for_namespace_termination,
)

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    platform_config: dict[str, Any],
) -> int:
    """
    Install the full MCP Gateway platform stack.

    Args:
        platform_config: Platform configuration dict from infrastructure.yaml
    """
    execute_tasks(locals())
    return 0


@task
def validate_config(args, ctx):
    """Validate platform configuration and resolve paths"""
    config = args.platform_config
    kustomize_base_raw = config.get("kustomize_base")
    if not kustomize_base_raw:
        raise ValueError(
            "platform.kustomize_base must be set when install_platform is true. "
            "It is normally set automatically by the prepare phase via "
            "clone_platform_repo(), or can be overridden with the "
            "MCP_GATEWAY_KUSTOMIZE_BASE environment variable."
        )
    ctx.kustomize_base = Path(kustomize_base_raw).expanduser().resolve()
    ctx.mcp_gateway_namespace = config.get("mcp_gateway_namespace", "mcp-system")
    ctx.gateway_namespace = config.get("gateway_namespace", "gateway-system")
    ctx.gateway_class_name = config.get("gateway_class_name", "istio")
    ctx.ctrl = config.get("mcp_gateway_controller", {})
    ctx.inst = config.get("mcp_gateway_instance", {})
    ctx.steps = config.get("steps", [])

    wait_for_namespace_termination([ctx.mcp_gateway_namespace, ctx.gateway_namespace])

    logger.info("Kustomize base: %s", ctx.kustomize_base)
    logger.info("MCP Gateway namespace: %s", ctx.mcp_gateway_namespace)
    logger.info("Gateway namespace: %s", ctx.gateway_namespace)

    return f"Config validated: {len(ctx.steps)} steps, kustomize_base={ctx.kustomize_base}"


@task
def install_service_mesh_operator(args, ctx):
    """Apply Service Mesh operator via kustomize and wait for CRDs"""
    step = find_step(ctx.steps, "service-mesh-operator")
    if not step:
        return "Step service-mesh-operator not found, skipping"

    kustomize_path = ctx.kustomize_base / step["path"]
    if not kustomize_path.exists():
        raise FileNotFoundError(f"Kustomize directory not found: {kustomize_path}")

    oc("apply", "-k", str(kustomize_path))

    if "wait_for_crds" in step:
        wait_for_crds_command.run(
            crd_names=step["wait_for_crds"],
            display_name="Service Mesh CRDs",
        )

    return f"Service Mesh operator installed from {kustomize_path}"


@task
def install_service_mesh_instance(args, ctx):
    """Apply Service Mesh instance (Istio, IstioCNI) via kustomize and wait for readiness"""
    step = find_step(ctx.steps, "service-mesh-instance")
    if not step:
        return "Step service-mesh-instance not found, skipping"

    kustomize_path = ctx.kustomize_base / step["path"]
    if not kustomize_path.exists():
        raise FileNotFoundError(f"Kustomize directory not found: {kustomize_path}")

    oc("apply", "-k", str(kustomize_path))

    if "wait_for_ready" in step:
        spec = step["wait_for_ready"]
        ctx._mesh_ready_spec = spec
        return (False, "Service Mesh instance applied, readiness checked in next task")

    return f"Service Mesh instance applied from {kustomize_path}"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_service_mesh_ready(args, ctx):
    """Wait for Istio instance to report Ready condition"""
    spec = getattr(ctx, "_mesh_ready_spec", None)
    if not spec:
        return "No readiness spec, skipping"

    payload = oc_get_json(
        spec["kind"],
        name=spec["name"],
        namespace=spec.get("namespace"),
        ignore_not_found=True,
    )
    if not payload:
        return (False, f"Waiting for {spec['kind']}/{spec['name']} to exist")

    conditions = payload.get("status", {}).get("conditions", [])
    if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
        return f"{spec['kind']}/{spec['name']} is Ready"

    return (False, f"Waiting for {spec['kind']}/{spec['name']} Ready condition")


@task
def install_connectivity_link_operator(args, ctx):
    """Apply Connectivity Link operator via kustomize and wait for CRDs"""
    step = find_step(ctx.steps, "connectivity-link-operator")
    if not step:
        return "Step connectivity-link-operator not found, skipping"

    kustomize_path = ctx.kustomize_base / step["path"]
    if not kustomize_path.exists():
        raise FileNotFoundError(f"Kustomize directory not found: {kustomize_path}")

    oc("apply", "-k", str(kustomize_path))

    if "wait_for_crds" in step:
        wait_for_crds_command.run(
            crd_names=step["wait_for_crds"],
            display_name="Connectivity Link CRDs",
        )

    return f"Connectivity Link operator installed from {kustomize_path}"


@task
def install_mcp_gateway_controller(args, ctx):
    """Ensure MCP Gateway namespaces exist (CRDs are installed by Helm)"""
    step = find_step(ctx.steps, "mcp-gateway-controller")
    if not step:
        return "Step mcp-gateway-controller not found, skipping"

    ensure_namespace(ctx.mcp_gateway_namespace)
    ensure_namespace(ctx.gateway_namespace)

    return "Namespaces ready (CRDs will be installed by Helm)"


@task
def install_connectivity_link_instance(args, ctx):
    """Create a Kuadrant CR (Connectivity Link instance) if not already present"""
    step = find_step(ctx.steps, "connectivity-link-instance")
    if not step:
        return "Step connectivity-link-instance not found, skipping"

    result = oc("get", "kuadrant", "-A", "-o", "name", check=False)
    if result.returncode == 0 and result.stdout.strip():
        return "Kuadrant CR already exists, skipping creation"

    ns = step.get("namespace", ctx.mcp_gateway_namespace)
    kuadrant_cr = {
        "apiVersion": "kuadrant.io/v1beta1",
        "kind": "Kuadrant",
        "metadata": {"name": "kuadrant", "namespace": ns},
    }
    artifact_path = env.ARTIFACT_DIR / "src" / "kuadrant-cr.yaml"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    oc_apply(artifact_path, kuadrant_cr)

    return f"Kuadrant CR created in {ns}"


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_kuadrant_components_ready(args, ctx):
    """Wait for Kuadrant-managed components (authorino, limitador) to be ready
    before installing the MCP Gateway Helm chart. The controller will delete the
    MCPGatewayExtension CR if these components aren't available yet."""
    ns = getattr(ctx, "mcp_gateway_namespace", "mcp-system")

    for component in ("authorino", "limitador-limitador"):
        dep = oc_get_json(
            "deployment",
            name=component,
            namespace=ns,
            ignore_not_found=True,
        )
        if not dep:
            return (False, f"Waiting for deployment/{component} to be created by Kuadrant operator")
        available = dep.get("status", {}).get("availableReplicas", 0)
        if not available or available < 1:
            return (
                False,
                f"deployment/{component} exists but not ready (availableReplicas={available})",
            )

    return "Kuadrant components (authorino, limitador) are ready"


@task
def install_mcp_gateway_instance(args, ctx):
    """Install MCP Gateway via Helm WITHOUT the MCPGatewayExtension CR.

    The MCPGatewayExtension is created in a separate step after the Gateway is
    programmed, to avoid a race condition where the controller deletes the CR
    because the Gateway isn't ready yet at first reconciliation.

    Supports two install modes:
    - Release: uses OCI chart ref + --version flag
    - Nightly: uses local chart path from cloned repo + image tag overrides
    """
    step = find_step(ctx.steps, "mcp-gateway-instance")
    if not step:
        return "Step mcp-gateway-instance not found, skipping"

    _ensure_helm()

    chart_path = ctx.inst.get("chart_path")
    image_tag = ctx.inst.get("image_tag")

    if chart_path:
        chart_ref = chart_path
        version_flag = []
    else:
        chart_ref = ctx.inst.get("chart_ref", "oci://ghcr.io/kuadrant/charts/mcp-gateway")
        chart_version = ctx.inst.get("version")
        version_flag = ["--version", chart_version] if chart_version else []

    ctx.mcp_host = _get_mcp_host()

    subprocess.run(
        ["helm", "uninstall", "mcp-gateway", "--namespace", ctx.mcp_gateway_namespace],
        check=False,
        capture_output=True,
        timeout=60,
    )

    cmd = [
        "helm",
        "install",
        "mcp-gateway",
        chart_ref,
        *version_flag,
        "--namespace",
        ctx.mcp_gateway_namespace,
        "--create-namespace",
        "--set",
        "controller.enabled=true",
        "--set",
        "gateway.create=true",
        "--set",
        "gateway.name=mcp-gateway",
        "--set",
        f"gateway.namespace={ctx.gateway_namespace}",
        "--set",
        f"gateway.publicHost={ctx.mcp_host}",
        "--set",
        "gateway.internalHostPattern=*.mcp.local",
        "--set",
        f"gateway.gatewayClassName={ctx.gateway_class_name}",
        "--set",
        "mcpGatewayExtension.create=false",
    ]

    if image_tag:
        cmd += ["--set", f"image.tag={image_tag}", "--set", f"imageController.tag={image_tag}"]

    subprocess.run(cmd, check=True, timeout=120)
    ctx.gateway_installed = True

    mode = "nightly" if chart_path else "release"
    return f"MCP Gateway Helm release installed ({mode}, host={ctx.mcp_host}, MCPGatewayExtension deferred)"


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_gateway_ready(args, ctx):
    """Wait for the MCP Gateway to be programmed"""
    if not getattr(ctx, "gateway_installed", False):
        return "Gateway not installed in this run, skipping"

    payload = oc_get_json(
        "gateway",
        name="mcp-gateway",
        namespace=ctx.gateway_namespace,
        ignore_not_found=True,
    )
    if not payload:
        return (False, "Waiting for Gateway/mcp-gateway to exist")

    conditions = payload.get("status", {}).get("conditions", [])
    programmed = any(
        c.get("type") == "Programmed" and c.get("status") == "True" for c in conditions
    )
    if programmed:
        return "Gateway/mcp-gateway is programmed"

    return (False, "Waiting for Gateway/mcp-gateway to be programmed")


@task
def create_mcp_gateway_extension(args, ctx):
    """Create the ReferenceGrant and MCPGatewayExtension CR now that the Gateway
    is programmed.

    Both resources are created here (instead of via Helm) to avoid the race
    condition where the controller sees the extension CR before the Gateway is
    ready and deletes it. The ReferenceGrant must exist first so the controller
    can resolve the cross-namespace Gateway reference.
    """
    if not getattr(ctx, "gateway_installed", False):
        return "Gateway not installed in this run, skipping"

    mcp_host = getattr(ctx, "mcp_host", _get_mcp_host())
    version = getattr(ctx, "inst", {}).get("version", "")
    vspec = _version_spec(version)
    src_dir = env.ARTIFACT_DIR / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    reference_grant = {
        "apiVersion": "gateway.networking.k8s.io/v1beta1",
        "kind": "ReferenceGrant",
        "metadata": {
            "name": "allow-mcp-gateway",
            "namespace": ctx.gateway_namespace,
        },
        "spec": {
            "from": [
                {
                    "group": vspec["api_group"],
                    "kind": "MCPGatewayExtension",
                    "namespace": ctx.mcp_gateway_namespace,
                }
            ],
            "to": [
                {
                    "group": "gateway.networking.k8s.io",
                    "kind": "Gateway",
                }
            ],
        },
    }
    oc_apply(src_dir / "reference-grant.yaml", reference_grant)

    spec: dict[str, Any] = {
        "targetRef": {
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": "mcp-gateway",
            "namespace": ctx.gateway_namespace,
            "sectionName": "mcp",
        },
        "publicHost": mcp_host,
        "backendPingIntervalSeconds": 60,
    }
    if vspec["has_private_host"]:
        spec["privateHost"] = f"mcp-gateway-istio.{ctx.gateway_namespace}.svc.cluster.local:8080"

    extension_cr = {
        "apiVersion": f"{vspec['api_group']}/v1alpha1",
        "kind": "MCPGatewayExtension",
        "metadata": {
            "name": vspec["extension_name"],
            "namespace": ctx.mcp_gateway_namespace,
        },
        "spec": spec,
    }
    oc_apply(src_dir / "mcp-gateway-extension.yaml", extension_cr)

    return (
        f"MCPGatewayExtension + ReferenceGrant created "
        f"(version={version or 'latest'}, api_group={vspec['api_group']}, host={mcp_host})"
    )


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_broker_ready(args, ctx):
    """Wait for the MCP Gateway broker deployment to have at least one ready replica"""
    if not getattr(ctx, "gateway_installed", False):
        return "Gateway not installed in this run, skipping"

    deployment = oc_get_json(
        "deployment",
        name="mcp-gateway",
        namespace=ctx.mcp_gateway_namespace,
        ignore_not_found=True,
    )
    if not deployment:
        return (False, "Waiting for mcp-gateway broker deployment to be created by controller")

    available = deployment.get("status", {}).get("availableReplicas", 0)
    if available and available >= 1:
        return "mcp-gateway broker is ready"

    return (
        False,
        f"mcp-gateway broker deployment exists but not ready (availableReplicas={available})",
    )


@always
@task
def capture_platform_state(args, ctx):
    """Capture platform component states for diagnostics"""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    ns = getattr(ctx, "mcp_gateway_namespace", "mcp-system")
    gw_ns = getattr(ctx, "gateway_namespace", "gateway-system")

    _capture_to_file(artifacts_dir / "pods-mcp-system.txt", "get", "pods", "-n", ns, "-o", "wide")
    _capture_to_file(
        artifacts_dir / "pods-gateway-system.txt", "get", "pods", "-n", gw_ns, "-o", "wide"
    )
    _capture_to_file(artifacts_dir / "csv-mcp-system.yaml", "get", "csv", "-n", ns, "-o", "yaml")
    _capture_to_file(
        artifacts_dir / "gateway.yaml", "get", "gateway", "mcp-gateway", "-n", gw_ns, "-o", "yaml"
    )
    _capture_to_file(artifacts_dir / "kuadrant.yaml", "get", "kuadrant", "-A", "-o", "yaml")

    return "Captured platform state"


# --- Helper functions ---


def _ensure_helm() -> None:
    """Download helm to /tmp if not already in PATH."""
    if shutil.which("helm"):
        return

    import os
    import platform
    import tarfile
    import urllib.request

    system = platform.system().lower()
    arch = platform.machine()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    arch = arch_map.get(arch, arch)

    url = f"https://get.helm.sh/helm-v3.17.3-{system}-{arch}.tar.gz"
    checksum_url = f"{url}.sha256sum"
    logger.info("Helm not found in PATH, downloading from %s", url)

    tar_path = Path("/tmp/helm.tar.gz")
    urllib.request.urlretrieve(url, tar_path)

    import hashlib

    expected_checksum = urllib.request.urlopen(checksum_url).read().decode().split()[0].strip()
    actual_checksum = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    if actual_checksum != expected_checksum:
        tar_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Helm checksum mismatch: expected {expected_checksum}, got {actual_checksum}"
        )

    with tarfile.open(tar_path) as tf:
        for member in tf.getmembers():
            if member.name.endswith("/helm"):
                member.name = "helm"
                tf.extract(member, "/tmp")
                break

    Path("/tmp/helm").chmod(0o755)
    tar_path.unlink(missing_ok=True)

    if "/tmp" not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"/tmp:{os.environ.get('PATH', '')}"

    logger.info("Helm installed to /tmp/helm")


def _get_mcp_host() -> str:
    """Resolve the MCP host from the cluster DNS base domain."""
    result = oc(
        "get",
        "dns",
        "cluster",
        "-o",
        "jsonpath={.spec.baseDomain}",
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return f"mcp.apps.{result.stdout.strip()}"
    return "mcp.apps.example.com"


def _capture_to_file(path: Path, *oc_args: str) -> None:
    """Best-effort capture of oc output to a file."""
    result = oc(*oc_args, check=False, log_stdout=False)
    if result.returncode == 0 and result.stdout:
        path.write_text(result.stdout, encoding="utf-8")


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse a semver string into a comparable tuple of ints."""
    import re

    return tuple(int(x) for x in re.findall(r"\d+", version)[:3])


def _version_gte(version: str, minimum: str) -> bool:
    """Check if a semver version string is >= minimum."""
    try:
        return _parse_version(version) >= _parse_version(minimum)
    except (ValueError, IndexError):
        logger.warning(
            "Could not parse version %r for comparison against %s, assuming older",
            version,
            minimum,
        )
        return False


def _version_spec(version: str) -> dict[str, Any]:
    """Return version-specific resource parameters.

    Known breakpoints (verified via ``helm template``):
      >=0.7.0  api_group=mcp.kuadrant.io, name=mcp-gateway-extension, +privateHost
       <0.7.0  api_group=mcp.kagenti.com,  name=mcp-gateway,           no privateHost
    """
    is_070_plus = _version_gte(version, "0.7.0") if version else True

    if is_070_plus:
        return {
            "api_group": "mcp.kuadrant.io",
            "extension_name": "mcp-gateway-extension",
            "has_private_host": True,
        }
    return {
        "api_group": "mcp.kagenti.com",
        "extension_name": "mcp-gateway",
        "has_private_host": False,
    }


if __name__ == "__main__":
    run.main()
