from __future__ import annotations

import json
import logging

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc, oc_get_json

logger = logging.getLogger("DSL")

ANNOTATION_KEY = "maas.opendatahub.io/payload-processing-type"
PRAXIS_ANNOTATION_VALUE = "praxis"
IPP_ANNOTATION_VALUE = "ipp"
DEPLOYMENTS_TO_CHECK = ["payload-processing", "payload-pre-processing"]


@entrypoint
def run(
    *,
    activate: bool,
    tenant_namespace: str = "models-as-a-service",
    tenant_name: str = "default-tenant",
    ingress_namespace: str = "openshift-ingress",
    kuadrant_namespace: str = "kuadrant-system",
    gateway_name: str = "maas-default-gateway",
    payload_processing_config: dict | None = None,
):
    """Activate or deactivate praxis payload processing on a MaasTenantConfig.

    Both directions annotate maas.opendatahub.io/payload-processing-type explicitly:
    activating sets it to praxis, deactivating opts back into ipp. Praxis is also the
    operator default when the annotation is absent.

    On every toggle (activate and deactivate), the pods of the gateway-side hot-path
    deployments (payload extprocs, gateway, authorino, limitador) are bumped so each
    benchmark endpoint starts from fresh pods.

    Args:
        activate: True to enable praxis, False to disable.
        tenant_namespace: Namespace of the MaasTenantConfig resource.
        tenant_name: Name of the MaasTenantConfig resource.
        ingress_namespace: Namespace where the gateway and payload-processing deployments live.
        kuadrant_namespace: Namespace where the authorino and limitador deployments live.
        gateway_name: Name of the MaaS Gateway resource.
        payload_processing_config: Scaling/resource config for payload-processing deployments
            (replicas, cpu_limit, memory_limit, cpu_request, memory_request).
            Applied identically via spec.payloadPreProcessing and spec.payloadProcessing
            on the CR.
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

    requests = {}
    if cfg.get("cpu_request") is not None:
        requests["cpu"] = str(cfg["cpu_request"])
    if cfg.get("memory_request") is not None:
        requests["memory"] = str(cfg["memory_request"])

    if limits or requests:
        resources = {}
        if limits:
            limits.setdefault("cpu", None)
            limits.setdefault("memory", None)
            resources["limits"] = limits
        else:
            resources["limits"] = None
        if requests:
            requests.setdefault("cpu", None)
            requests.setdefault("memory", None)
            resources["requests"] = requests
        else:
            resources["requests"] = None
        payload_processing["resources"] = resources
    else:
        payload_processing["resources"] = None

    # payloadPreProcessing and payloadProcessing share the same schema and must be
    # identically configured
    patch = json.dumps(
        {
            "spec": {
                "payloadPreProcessing": payload_processing,
                "payloadProcessing": payload_processing,
            }
        }
    )

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

    return (
        f"Patched MaasTenantConfig {args.tenant_name} "
        f"payloadPreProcessing/payloadProcessing: {payload_processing}"
    )


@task
def toggle_annotation(args, ctx):
    """Annotate MaasTenantConfig with praxis or ipp payload-processing-type.

    Praxis is also the operator default when the annotation is absent; annotating
    with ipp opts back into the previous payload processing.
    """
    value = PRAXIS_ANNOTATION_VALUE if args.activate else IPP_ANNOTATION_VALUE
    oc(
        "annotate",
        "maastenantconfig",
        args.tenant_name,
        "-n",
        args.tenant_namespace,
        f"{ANNOTATION_KEY}={value}",
        "--overwrite",
    )
    return f"Set {ANNOTATION_KEY}={value} on {args.tenant_name}"


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


@task
def bump_hot_path_pods(args, ctx):
    """Delete the pods of the gateway-side hot-path deployments.

    The deployments recreate their pods, so each benchmark endpoint starts from fresh
    pods (no leftover connection pools, auth caches or log windows from previous runs).
    Runs after wait_for_deployment_images so it does not race the operator's image roll.
    """
    from projects.ag_praxis.toolbox.capture_maas_praxis.main import build_hot_path_targets

    ctx.hot_path_targets = build_hot_path_targets(
        args.ingress_namespace, args.kuadrant_namespace, args.gateway_name
    )

    for target in ctx.hot_path_targets:
        oc(
            "delete",
            "pod",
            "-l",
            target.selector,
            "-n",
            target.namespace,
            "--ignore-not-found",
        )

    names = ", ".join(f"{t.name} ({t.namespace})" for t in ctx.hot_path_targets)
    return f"Bumped pods of: {names}"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_hot_path_deployments_ready(args, ctx):
    """Wait for all hot-path deployments to be ready after the bump."""
    not_ready = []
    for target in ctx.hot_path_targets:
        deploy = oc_get_json(
            "deployment",
            name=target.name,
            namespace=target.namespace,
            ignore_not_found=True,
        )
        if deploy is None:
            not_ready.append(f"{target.name} does not exist in {target.namespace}")
            continue

        ready = deploy.get("status", {}).get("readyReplicas", 0)
        desired = deploy.get("spec", {}).get("replicas", 1)
        if ready < desired:
            not_ready.append(f"{target.name} not ready yet ({ready}/{desired} replicas)")

    if not_ready:
        return False, "; ".join(not_ready)
    return "All hot-path deployments ready after bump"


@task
def log_deployment_status(args, ctx):
    """Log the current status of payload-processing deployments."""
    oc(
        "get",
        "deployments",
        "-n",
        args.ingress_namespace,
        "-owide",
        log_stdout=True,
    )
    return "Logged deployment status"


@always
@task
def capture_state(args, ctx):
    """Capture MaaS praxis state for debugging (runs even on failure)."""
    from projects.ag_praxis.toolbox.capture_maas_praxis import main as capture_maas_praxis_command

    capture_maas_praxis_command.run(
        ingress_namespace=args.ingress_namespace,
        tenant_namespace=args.tenant_namespace,
        tenant_name=args.tenant_name,
        kuadrant_namespace=args.kuadrant_namespace,
        gateway_name=args.gateway_name,
    )
    return "Captured MaaS praxis state"


if __name__ == "__main__":
    run.main()
