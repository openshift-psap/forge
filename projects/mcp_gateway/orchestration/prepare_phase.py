"""
Prepare phase for MCP Gateway performance tests.

1. Clone platform manifests from the upstream mcp-gateway GitHub repo
2. Install the MCP Gateway platform at the version specified by MCP_GATEWAY_VERSION
3. Ensure test namespace exists

Supports two modes:
- Release mode: MCP_GATEWAY_VERSION=0.7.0 (semver tag)
- Nightly mode: MCP_GATEWAY_VERSION=<40-char commit SHA> (uses pre-built images from ghcr.io)

Usage:
    MCP_GATEWAY_VERSION=0.7.0 python -m projects.mcp_gateway.orchestration.ci prepare
    MCP_GATEWAY_VERSION=<SHA> python -m projects.mcp_gateway.orchestration.ci prepare
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from projects.core.library import config
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.mcp_gateway.orchestration.runtime_config import cfg
from projects.mcp_gateway.toolbox.install_platform import main as install_platform_mod
from projects.mcp_gateway.toolbox.platform_helpers import clone_platform_repo

logger = logging.getLogger(__name__)

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def run() -> int:
    version = config.project.get_config(
        "infrastructure.mcp_gateway_version", None, print=False, warn=False
    ) or os.environ.get("MCP_GATEWAY_VERSION")
    if not version:
        raise RuntimeError(
            "MCP Gateway version not set. Use /version directive in PR comment, "
            "set infrastructure.mcp_gateway_version in config, or set "
            "MCP_GATEWAY_VERSION environment variable."
        )

    is_nightly = _is_commit_sha(version)
    namespace = cfg.get_namespace()

    logger.info("=== MCP Gateway Prepare Phase ===")
    logger.info("Version: %s", version)
    logger.info("Mode: %s", "nightly (commit SHA)" if is_nightly else "release")
    logger.info("Namespace: %s", namespace)

    platform_cfg = cfg.get_platform_config()
    platform_cfg.setdefault("mcp_gateway_instance", {})["version"] = version

    if not platform_cfg.get("kustomize_base"):
        repo_url = platform_cfg.get("platform_repo")
        subdir = platform_cfg.get("platform_repo_subdir")
        clone_kwargs: dict = {"version": version}
        if repo_url:
            clone_kwargs["repo_url"] = repo_url
        if subdir:
            clone_kwargs["subdir"] = subdir
        kustomize_base = clone_platform_repo(**clone_kwargs)
        platform_cfg["kustomize_base"] = str(kustomize_base)

    if is_nightly:
        image_tag = f"sha-{version}"
        chart_path = _add_charts_to_sparse_checkout(platform_cfg["kustomize_base"])
        if chart_path:
            platform_cfg["mcp_gateway_instance"]["chart_path"] = str(chart_path)
            platform_cfg["mcp_gateway_instance"]["image_tag"] = image_tag
            logger.info("Nightly: using local chart at %s", chart_path)
            logger.info("Nightly: image tag override = %s", image_tag)
        else:
            raise RuntimeError(
                f"Failed to checkout charts/mcp-gateway from clone. "
                f"Cannot install nightly build for commit {version}."
            )

    install_platform_mod.run(platform_config=platform_cfg)

    ensure_namespace(
        namespace,
        labels={
            "app.kubernetes.io/managed-by": "forge",
            "forge.openshift.io/project": "mcp_gateway",
        },
    )

    logger.info("=== Prepare phase complete ===")
    return 0


# --- Helper functions ---


def _is_commit_sha(version: str) -> bool:
    return bool(_SHA_PATTERN.match(version))


def _add_charts_to_sparse_checkout(kustomize_base: str) -> Path | None:
    """Add charts/mcp-gateway to the existing sparse checkout so we can helm install from source."""
    repo_dir = Path(kustomize_base)
    while repo_dir != repo_dir.parent:
        if (repo_dir / ".git").exists():
            break
        repo_dir = repo_dir.parent
    else:
        return None

    subprocess.run(
        ["git", "sparse-checkout", "add", "charts/mcp-gateway"],
        cwd=str(repo_dir),
        check=True,
        timeout=30,
    )
    chart_path = repo_dir / "charts" / "mcp-gateway"
    if chart_path.is_dir():
        return chart_path
    return None
