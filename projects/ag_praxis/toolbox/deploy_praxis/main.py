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
    """Delete existing Praxis Deployment (owned resources are garbage-collected)."""
    best_effort_oc(
        "delete",
        "deployment",
        "praxis",
        "-n",
        args.namespace,
        "--ignore-not-found",
    )
    return "Deleted previous Praxis Deployment"


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


def _get_deployment_uid(namespace):
    deploy = oc_get_json("deployment", name="praxis", namespace=namespace)
    return deploy["metadata"]["uid"]


@task
def apply_configmap(args, ctx):
    """Apply the Praxis configuration ConfigMap owned by the Deployment."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    from urllib.parse import urlparse

    parsed = urlparse(args.model_endpoint)
    host_port = f"{parsed.hostname}:{parsed.port or 8000}"

    deployment_uid = _get_deployment_uid(args.namespace)

    praxis_config = render_template(
        "praxis-ai.yaml.j2",
        context={
            "model_endpoint": host_port,
            "model_host": parsed.hostname,
        },
    )

    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "praxis-config",
            "namespace": args.namespace,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "praxis",
                    "uid": deployment_uid,
                }
            ],
        },
        "data": {
            "praxis-ai.yaml": praxis_config,
        },
    }

    oc_apply(src_dir / "configmap.yaml", manifest)
    return f"Applied Praxis ConfigMap pointing to {host_port}"


@task
def apply_service(args, ctx):
    """Apply the Praxis Service owned by the Deployment."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    deployment_uid = _get_deployment_uid(args.namespace)

    manifest = yaml.safe_load(
        render_template(
            "service.yaml.j2",
            context={
                "namespace": args.namespace,
                "deployment_uid": deployment_uid,
            },
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


@task
def apply_servicemonitor(args, ctx):
    """Apply the Praxis ServiceMonitor owned by the Deployment."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    deployment_uid = _get_deployment_uid(args.namespace)

    manifest = yaml.safe_load(
        render_template(
            "servicemonitor.yaml.j2",
            context={
                "namespace": args.namespace,
                "deployment_uid": deployment_uid,
            },
        )
    )
    oc_apply(src_dir / "servicemonitor.yaml", manifest)
    return "Applied Praxis ServiceMonitor (owned by Deployment)"


if __name__ == "__main__":
    run.main()
