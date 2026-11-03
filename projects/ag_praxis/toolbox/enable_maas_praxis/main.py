from __future__ import annotations

import json
import logging

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_get_json

logger = logging.getLogger("DSL")

ANNOTATION_KEY = "maas.opendatahub.io/payload-processing-type"
ANNOTATION_VALUE = "praxis"
DEPLOYMENTS_TO_CHECK = ["payload-processing", "payload-pre-processing"]


@entrypoint
def run(
    *,
    activate: bool,
    tenant_namespace: str = "models-as-a-service",
    tenant_name: str = "default-tenant",
    ingress_namespace: str = "openshift-ingress",
    payload_processing_config: dict | None = None,
):
    """Activate or deactivate praxis payload processing on a MaasTenantConfig.

    Args:
        activate: True to enable praxis, False to disable.
        tenant_namespace: Namespace of the MaasTenantConfig resource.
        tenant_name: Name of the MaasTenantConfig resource.
        ingress_namespace: Namespace where payload-processing deployments live.
        payload_processing_config: Scaling/resource config for payload-processing deployments
            (replicas, cpu_limit, memory_limit). Applied via spec.payloadProcessing on the CR.
    """
    return execute_tasks(locals())


@task
def configure_payload_processing(args, ctx):
    """Patch MaasTenantConfig with payload processing scaling/resource config.

    Non-None values are set on the CR; None values are removed (null in merge patch).
    """
    if not args.activate:
        return "Skipped: praxis is being deactivated"

    cfg = args.payload_processing_config
    if not cfg:
        return "No payload processing config to apply"

    payload_processing = {}

    payload_processing["replicas"] = cfg.get("replicas")

    limits = {}
    if cfg.get("cpu_limit") is not None:
        limits["cpu"] = str(cfg["cpu_limit"])
    if cfg.get("memory_limit") is not None:
        limits["memory"] = str(cfg["memory_limit"])

    if limits:
        limits.setdefault("cpu", None)
        limits.setdefault("memory", None)
        payload_processing["resources"] = {"limits": limits}
    else:
        payload_processing["resources"] = None

    patch = json.dumps({"spec": {"payloadProcessing": payload_processing}})

    oc(
        "patch",
        "maastenantconfig",
        args.tenant_name,
        "-n",
        args.tenant_namespace,
        "--type=merge",
        "-p",
        patch,
    )

    return f"Patched MaasTenantConfig {args.tenant_name} payloadProcessing: {payload_processing}"


@task
def toggle_annotation(args, ctx):
    """Set or remove the praxis annotation on MaasTenantConfig."""
    if args.activate:
        oc(
            "annotate",
            "maastenantconfig",
            args.tenant_name,
            "-n",
            args.tenant_namespace,
            f"{ANNOTATION_KEY}={ANNOTATION_VALUE}",
            "--overwrite",
        )
        return f"Set {ANNOTATION_KEY}={ANNOTATION_VALUE} on {args.tenant_name}"

    oc(
        "annotate",
        "maastenantconfig",
        args.tenant_name,
        "-n",
        args.tenant_namespace,
        f"{ANNOTATION_KEY}-",
    )
    return f"Removed {ANNOTATION_KEY} from {args.tenant_name}"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_for_deployment_images(args, ctx):
    """Wait for payload-processing deployments to reflect the praxis toggle."""
    for deploy_name in DEPLOYMENTS_TO_CHECK:
        deploy = oc_get_json(
            "deployment",
            name=deploy_name,
            namespace=args.ingress_namespace,
            ignore_not_found=True,
        )
        if deploy is None:
            if args.activate:
                return False, f"{deploy_name} does not exist yet"
            continue

        containers = (
            deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
        images = [c.get("image", "") for c in containers]
        has_praxis = any("praxis" in img for img in images)

        if args.activate and not has_praxis:
            return False, f"{deploy_name} does not yet have praxis image: {images}"
        if not args.activate and has_praxis:
            return False, f"{deploy_name} still has praxis image: {images}"

        ready = deploy.get("status", {}).get("readyReplicas", 0)
        desired = deploy.get("spec", {}).get("replicas", 1)
        if ready < desired:
            return False, f"{deploy_name} not ready yet ({ready}/{desired} replicas)"

    action = "activated" if args.activate else "deactivated"
    return f"Praxis {action}: all deployments ready with expected images"


if __name__ == "__main__":
    run.main()
