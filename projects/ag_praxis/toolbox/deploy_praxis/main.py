#!/usr/bin/env python3

from __future__ import annotations

import logging

import yaml

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import (
    best_effort_oc,
    oc_apply,
    oc_get_json,
    oc_resource_exists,
)

logger = logging.getLogger("DSL")

PRAXIS_IMAGE_REPO = "ghcr.io/praxis-proxy/ai"


@entrypoint
def run(
    *,
    namespace: str,
    model_endpoint: str,
    praxis_version: str,
    replicas: int = 2,
):
    """Deploy the Praxis proxy in front of an RHOAI model-serving endpoint.

    Args:
        namespace: Namespace where Praxis will be deployed.
        model_endpoint: Full endpoint URL for the backend model service.
        praxis_version: Pinned Praxis image version tag.
        replicas: Number of Praxis pod replicas.
    """
    return execute_tasks(locals())


@task
def delete_previous(args, ctx):
    """Delete existing Praxis Deployment and ConfigMap."""
    best_effort_oc(
        "delete",
        "deployment",
        "praxis",
        "-n",
        args.namespace,
        "--ignore-not-found",
    )
    best_effort_oc(
        "delete",
        "configmap",
        "praxis-config",
        "-n",
        args.namespace,
        "--ignore-not-found",
    )
    return "Deleted previous Praxis Deployment and ConfigMap"


@task
def ensure_namespace(args, ctx):
    """Ensure the Praxis namespace exists."""
    if oc_resource_exists("namespace", args.namespace):
        return f"Namespace {args.namespace} already exists"

    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    manifest = yaml.safe_load(
        render_template(
            "namespace.yaml.j2",
            context={"namespace": args.namespace},
        )
    )
    oc_apply(src_dir / "namespace.yaml", manifest)
    return f"Created namespace {args.namespace}"


@task
def apply_configmap(args, ctx):
    """Apply the Praxis configuration ConfigMap."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    manifest = yaml.safe_load(
        render_template(
            "configmap.yaml.j2",
            context={
                "namespace": args.namespace,
                "model_endpoint": args.model_endpoint,
                "model_host": args.model_endpoint.rsplit(":", 1)[0],
            },
        )
    )
    oc_apply(src_dir / "configmap.yaml", manifest)
    return f"Applied Praxis ConfigMap pointing to {args.model_endpoint}"


@task
def apply_deployment(args, ctx):
    """Apply the Praxis Deployment."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    image = f"{PRAXIS_IMAGE_REPO}:{args.praxis_version}"

    manifest = yaml.safe_load(
        render_template(
            "deployment.yaml.j2",
            context={
                "namespace": args.namespace,
                "image": image,
                "replicas": args.replicas,
            },
        )
    )
    oc_apply(src_dir / "deployment.yaml", manifest)
    return f"Applied Praxis Deployment with image {image}"


@task
def apply_service(args, ctx):
    """Apply the Praxis Service."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    manifest = yaml.safe_load(
        render_template(
            "service.yaml.j2",
            context={"namespace": args.namespace},
        )
    )
    oc_apply(src_dir / "service.yaml", manifest)
    return "Applied Praxis Service"


@retry(attempts=60, delay=5, backoff=1.0)
@task
def wait_for_rollout(args, ctx):
    """Wait for the Praxis Deployment to be available."""
    deploy = oc_get_json("deployment", name="praxis", namespace=args.namespace)
    conditions = {c["type"]: c["status"] for c in deploy.get("status", {}).get("conditions", [])}

    if conditions.get("Available") != "True":
        return False

    return "Praxis Deployment is available"


if __name__ == "__main__":
    run.main()
