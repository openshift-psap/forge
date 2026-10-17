from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc

logger = logging.getLogger("DSL")


@entrypoint
def run(
    *,
    namespace: str,
    model_names: list[str],
):
    """Delete ExternalModel CRs, HTTPRoutes, and credential secrets created by setup_maas_external_models.

    Args:
        namespace: Namespace where the resources live.
        model_names: List of model names to clean up.
    """

    if not model_names:
        raise ValueError("model_names must not be empty")

    return execute_tasks(locals())


@task
def delete_httproutes(args, ctx):
    """Delete HTTPRoutes for each model."""
    for model_name in args.model_names:
        best_effort_oc("delete", "httproute", f"perf-{model_name}", "-n", args.namespace)

    return f"Deleted {len(args.model_names)} HTTPRoutes"


@task
def delete_external_models(args, ctx):
    """Delete ExternalModel CRs for each model."""
    for model_name in args.model_names:
        best_effort_oc(
            "delete", "externalmodel.maas.opendatahub.io", model_name, "-n", args.namespace
        )

    return f"Deleted {len(args.model_names)} ExternalModel CRs"


@task
def delete_credential_secrets(args, ctx):
    """Delete dummy credential secrets for each model."""
    for model_name in args.model_names:
        best_effort_oc("delete", "secret", f"{model_name}-creds", "-n", args.namespace)

    return f"Deleted {len(args.model_names)} credential secrets"


if __name__ == "__main__":
    run.main()
