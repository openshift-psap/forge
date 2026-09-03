"""Resolve benchmark configurations from the benchconf package.

This module bridges the benchconf package (external benchmark config repository)
with forge's benchmark runner. It reads the referenced config YAML and returns
its content for the toolbox to embed in the GuideLLM container.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve_config_content(benchmark: dict) -> str | None:
    """Read the benchconf config YAML content if the benchmark references one.

    Args:
        benchmark: Benchmark configuration dict from workloads.yaml.
            If it contains a ``benchconf`` key (e.g. ``"llm-d/concurrent-1k-1k"``),
            the corresponding config file is read from the benchconf package.

    Returns:
        The raw YAML content as a string, or None if no benchconf reference.
    """
    benchconf_ref = benchmark.get("benchconf")
    if not benchconf_ref:
        return None

    try:
        import benchconf
    except ImportError as exc:
        raise ImportError(
            "benchconf package is required for benchconf-based benchmarks. "
            "Install with: pip install 'benchconf @ git+https://github.com/openshift-psap/benchconf'"
        ) from exc

    parts = benchconf_ref.split("/", 1)
    if len(parts) != 2:
        raise ValueError(
            f"Invalid benchconf reference '{benchconf_ref}'. "
            "Expected format: 'suite/name' (e.g. 'llm-d/concurrent-1k-1k')"
        )

    suite, name = parts
    config_path = benchconf.get_config(suite, name)
    logger.info("Resolved benchconf '%s' to %s", benchconf_ref, config_path)
    return config_path.read_text()
