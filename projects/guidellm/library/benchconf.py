"""Resolve benchmark configurations from the benchconf package.

This module bridges the benchconf package (external benchmark config repository)
with forge's benchmark runner. It resolves a benchconf reference to a local
``Path`` that the toolbox can read and embed in the GuideLLM container.
"""

from __future__ import annotations

import logging
from pathlib import Path

from projects.core.library.config import requires

logger = logging.getLogger(__name__)


@requires(is_enabled="benchconf.enabled")
def _is_enabled(_cfg) -> bool:
    """Return whether benchconf resolution is enabled in the project config."""
    return _cfg.is_enabled


@requires(custom_version="benchconf.custom_version")
def maybe_install_custom_version(_cfg) -> None:
    """Install a custom benchconf version if configured.

    Reads ``benchconf.custom_version`` from the project config and, when
    enabled, pip-installs the specified repo/version at runtime.
    """
    custom = _cfg.custom_version
    if not custom.get("enabled"):
        return

    import subprocess
    import sys

    spec = f"benchconf @ {custom['repo']}@{custom['version']}"
    logger.info("Installing benchconf: %s", spec)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", spec])


def save_version() -> None:
    """Save the installed benchconf version and commit to CI metadata."""
    import json
    from importlib.metadata import distribution

    from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
    from projects.core.library import env

    dist = distribution("benchconf")
    info: dict[str, str | None] = {"version": dist.metadata["Version"]}

    direct_url_file = dist._path / "direct_url.json"
    if direct_url_file.exists():
        direct_url = json.loads(direct_url_file.read_text())
        vcs_info = direct_url.get("vcs_info", {})
        info["commit"] = vcs_info.get("commit_id")
        info["url"] = direct_url.get("url")

    dest = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "benchconf.version.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(info, indent=2))
    logger.info("Saved benchconf version to %s: %s", dest, info)


def resolve_config_path(benchconf_ref: str) -> Path:
    """Resolve a benchconf reference to a local config file path.

    Args:
        benchconf_ref: Reference in ``suite/name`` format
            (e.g. ``"llm-d/concurrent-1k-1k"``).

    Returns:
        Path to the resolved YAML config file on the local filesystem.
    """
    try:
        import benchconf
    except ImportError as exc:
        raise ImportError(
            "benchconf package is required for benchconf-based benchmarks. "
            "Install with: pip install 'benchconf @ git+https://github.com/openshift-psap/benchconf'"
        ) from exc

    parts = benchconf_ref.split("/")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid benchconf reference '{benchconf_ref}'. "
            "Expected format: 'suite/name' (e.g. 'llm-d/concurrent-1k-1k')"
        )

    suite, name = parts
    config_path = benchconf.get_config(suite, name)
    logger.info("Resolved benchconf '%s' to %s", benchconf_ref, config_path)
    return config_path
