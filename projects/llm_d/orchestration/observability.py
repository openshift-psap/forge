"""Resolve and validate run-scoped observability before cluster changes."""

from collections.abc import Iterable

from projects.core.library import config


def get_observability_context(
    workload_namespaces: Iterable[str],
) -> tuple[dict, str] | None:
    settings = config.project.get_config("observability", {}, print=False, warn=False)
    if not settings.get("enabled", False):
        return None

    from projects.observability.library.runtime import (
        resolve_run_id,
        validate_observability_run,
    )

    namespaces = sorted(set(workload_namespaces))
    if not namespaces:
        raise ValueError("Enabled observability requires a llm_d workload namespace")
    if len(namespaces) != 1:
        raise ValueError(
            f"Enabled observability supports one llm_d workload namespace per run; got {namespaces}"
        )

    run_id = resolve_run_id(settings)
    validate_observability_run(settings, workload_namespace=namespaces[0], run_id=run_id)
    return settings, run_id
