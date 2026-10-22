#!/usr/bin/env python3

from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc, oc

logger = logging.getLogger("DSL")


@entrypoint
def run(
    *,
    model_namespace: str,
    tenant_namespace: str,
    llmisvc_name: str,
    model_ref_name: str = "",
    model_ref_kind: str = "LLMInferenceService",
):
    """Unregister a model from MaaS by deleting MaaSSubscription, MaaSAuthPolicy, and MaaSModelRef.

    For ExternalModel kind, also deletes the ExternalModel CR and credential secret.

    Args:
        model_namespace: Namespace where the MaaSModelRef lives.
        tenant_namespace: Namespace where MaaSAuthPolicy and MaaSSubscription live.
        llmisvc_name: Name of the LLMInferenceService that was referenced.
        model_ref_name: Name of the MaaSModelRef resource. Defaults to llmisvc_name.
        model_ref_kind: Kind of resource the MaaSModelRef references ("LLMInferenceService" or "ExternalModel").
    """
    return execute_tasks(locals())


@task
def resolve_names(args, ctx):
    """Resolve default names from parameters."""
    ctx.model_ref_name = args.model_ref_name or args.llmisvc_name
    ctx.is_external = args.model_ref_kind == "ExternalModel"
    return f"Model ref name: {ctx.model_ref_name}"


@task
def capture_maas_resources(args, ctx):
    """Capture the YAML of all MaaS resources before deletion."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if ctx.is_external:
        oc(
            "get",
            "externalmodel.maas.opendatahub.io",
            ctx.model_ref_name,
            "-n",
            args.model_namespace,
            "-o",
            "yaml",
            check=False,
            log_stdout=False,
            stdout_dest=artifacts_dir / "externalmodel-maas.yaml",
        )
        oc(
            "get",
            "externalprovider.inference.opendatahub.io",
            f"{ctx.model_ref_name}-provider",
            "-n",
            args.model_namespace,
            "-o",
            "yaml",
            check=False,
            log_stdout=False,
            stdout_dest=artifacts_dir / "externalprovider.yaml",
        )
        oc(
            "get",
            "externalmodel.inference.opendatahub.io",
            ctx.model_ref_name,
            "-n",
            args.model_namespace,
            "-o",
            "yaml",
            check=False,
            log_stdout=False,
            stdout_dest=artifacts_dir / "externalmodel-inference.yaml",
        )

    oc(
        "get",
        "maasmodelref",
        ctx.model_ref_name,
        "-n",
        args.model_namespace,
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "maasmodelref.yaml",
    )
    oc(
        "get",
        "maasauthpolicy",
        f"{args.model_namespace}-{ctx.model_ref_name}-access",
        "-n",
        args.tenant_namespace,
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "maasauthpolicy.yaml",
    )
    oc(
        "get",
        "maassubscription",
        f"{args.model_namespace}-{ctx.model_ref_name}-subscription",
        "-n",
        args.tenant_namespace,
        "-o",
        "yaml",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "maassubscription.yaml",
    )

    return f"Captured MaaS resource YAMLs for {ctx.model_ref_name}"


@task
def delete_maas_subscription(args, ctx):
    """Delete the MaaSSubscription."""
    best_effort_oc(
        "delete",
        "maassubscription",
        f"{args.model_namespace}-{ctx.model_ref_name}-subscription",
        "-n",
        args.tenant_namespace,
        "--ignore-not-found",
    )
    return f"Deleted MaaSSubscription {ctx.model_ref_name}-subscription"


@task
def delete_maas_auth_policy(args, ctx):
    """Delete the MaaSAuthPolicy."""
    best_effort_oc(
        "delete",
        "maasauthpolicy",
        f"{args.model_namespace}-{ctx.model_ref_name}-access",
        "-n",
        args.tenant_namespace,
        "--ignore-not-found",
    )
    return f"Deleted MaaSAuthPolicy {ctx.model_ref_name}-access"


@task
def delete_maas_model_ref(args, ctx):
    """Delete the MaaSModelRef."""
    best_effort_oc(
        "delete",
        "maasmodelref",
        ctx.model_ref_name,
        "-n",
        args.model_namespace,
        "--ignore-not-found",
    )
    return f"Deleted MaaSModelRef {ctx.model_ref_name}"


@task
def delete_external_model(args, ctx):
    """Delete the ExternalModel and credential secret (only for ExternalModel kind)."""
    if not ctx.is_external:
        return "Skipped (not ExternalModel)"

    best_effort_oc(
        "delete",
        "externalmodel.inference.opendatahub.io",
        ctx.model_ref_name,
        "-n",
        args.model_namespace,
        "--ignore-not-found",
    )
    best_effort_oc(
        "delete",
        "externalprovider.inference.opendatahub.io",
        f"{ctx.model_ref_name}-provider",
        "-n",
        args.model_namespace,
        "--ignore-not-found",
    )
    best_effort_oc(
        "delete",
        "externalmodel.maas.opendatahub.io",
        ctx.model_ref_name,
        "-n",
        args.model_namespace,
        "--ignore-not-found",
    )
    best_effort_oc(
        "delete",
        "httproute",
        f"{ctx.model_ref_name}-route",
        "-n",
        args.model_namespace,
        "--ignore-not-found",
    )
    best_effort_oc(
        "delete",
        "secret",
        f"{ctx.model_ref_name}-creds",
        "-n",
        args.model_namespace,
        "--ignore-not-found",
    )
    return f"Deleted ExternalModel, ExternalProvider, credential secret for {ctx.model_ref_name}"


if __name__ == "__main__":
    run.main()
