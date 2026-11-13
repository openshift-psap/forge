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

EXTPROC_MAX_REQUESTS_ANNOTATION_KEY = "maas.opendatahub.io/experimental-extproc-max-requests"
# Controller ceiling (MaxExtProcMaxRequests). Values above it are REJECTED by the
# controller with a silent fallback to the default 8 -- not clamped -- so the range
# is enforced here, before annotating.
MAX_EXTPROC_MAX_REQUESTS = 1024


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
    extproc_max_requests: int | None = None,
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
        extproc_max_requests: ExtProc concurrency budget to annotate on the MaasTenantConfig
            (maas.opendatahub.io/experimental-extproc-max-requests). The operator default
            is 8 (ai-gateway-controller PR #217 hardening): above 8 concurrent in-flight
            ext_proc calls the gateway sheds requests with an empty HTTP 500 before auth.
            Valid range 1-1024. The effective limits are verified against the gateway's
            live Envoy config (see verify_extproc_limits).
    """
    if (
        extproc_max_requests is not None
        and not 1 <= extproc_max_requests <= MAX_EXTPROC_MAX_REQUESTS
    ):
        raise ValueError(
            f"Invalid extproc_max_requests: {extproc_max_requests!r}. "
            f"Valid range: 1-{MAX_EXTPROC_MAX_REQUESTS}. "
            "Out-of-range values are silently rejected by the controller "
            "(fallback to the default 8), so they are refused here instead."
        )

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


@task
def set_extproc_max_requests(args, ctx):
    """Annotate MaasTenantConfig with the ExtProc concurrency budget.

    The budget is enforced by Envoy circuit breakers on the ext_proc clusters:
    above it, requests are shed with an instant empty HTTP 500 *before* auth
    (ext_proc_error gRPC 14, reset_reason: overflow). With the default budget
    of 8, throughput is pinned at 8 / extproc_latency regardless of user count.

    The controller writes the annotated value into all four limits (max_requests,
    max_pending_requests, max_connections, max_concurrent_streams) on all ext_proc
    clusters. The annotation is only read on reconcile, which for annotation-only
    changes may wait for the controller resync -- verify_extproc_limits waits for
    the effective config to catch up.
    """
    if not args.activate:
        return "Skipped: praxis is being deactivated (annotation left as-is)"
    if args.extproc_max_requests is None:
        return "Skipped: no extproc_max_requests configured, keeping operator default"

    oc(
        "annotate",
        "maastenantconfig",
        args.tenant_name,
        "-n",
        args.tenant_namespace,
        f"{EXTPROC_MAX_REQUESTS_ANNOTATION_KEY}={args.extproc_max_requests}",
        "--overwrite",
    )
    return (
        f"Set {EXTPROC_MAX_REQUESTS_ANNOTATION_KEY}={args.extproc_max_requests} "
        f"on {args.tenant_name}"
    )


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


@retry(attempts=60, delay=10, backoff=1.0)
@task
def verify_extproc_limits(args, ctx):
    """Verify the effective ExtProc circuit-breaker limits on the live gateway.

    Ground truth is the gateway pod's Envoy config_dump, not the CRs: an
    out-of-range annotation value silently falls back to the default 8, and
    annotation-only changes may wait for the controller resync before being
    reconciled into the EnvoyFilter. Fails the run if the budget never takes
    effect, instead of letting the benchmark silently run against a budget
    that sheds all traffic above 8 concurrent requests.

    Envoy stats must NOT be used for this check: Istio's stats_matcher means
    most stats are never instantiated, so "no output" is indistinguishable
    from "not configured". config_dump is unaffected.
    """
    if not args.activate or args.extproc_max_requests is None:
        return "Skipped: no extproc budget to verify"

    from projects.ag_praxis.toolbox.capture_maas_praxis.main import (
        _resolve_gateway_deployment_name,
    )

    deployment = _resolve_gateway_deployment_name(args.gateway_name, args.ingress_namespace)

    result = oc(
        "exec",
        "-n",
        args.ingress_namespace,
        f"deploy/{deployment}",
        "--",
        "pilot-agent",
        "request",
        "GET",
        "config_dump?resource=dynamic_active_clusters",
        check=False,
        log_stdout=False,
    )
    if result.returncode != 0:
        return False, f"config_dump fetch failed on deploy/{deployment}: rc={result.returncode}"

    clusters = [
        config_entry["cluster"]
        for config_entry in json.loads(result.stdout).get("configs", [])
        if "extproc" in config_entry.get("cluster", {}).get("name", "")
    ]
    if not clusters:
        return False, "No extproc clusters in the gateway Envoy config yet"

    expected = args.extproc_max_requests
    problems = []
    seen = []
    for cluster in clusters:
        name = cluster["name"]
        # A threshold list longer than one entry means a botched MERGE appended
        # to the repeated field -- the original limits may still be in force.
        thresholds = cluster.get("circuit_breakers", {}).get("thresholds") or []
        if not thresholds:
            if "payload" in name:
                problems.append(f"{name}: no circuit_breakers thresholds")
            continue

        seen.append(name)
        for entry in thresholds:
            limits = {
                field: entry.get(field)
                for field in ("max_requests", "max_pending_requests", "max_connections")
            }
            mismatched = {
                field: value
                for field, value in limits.items()
                if value is not None and value != expected
            }
            if "payload" in name and (mismatched or len(thresholds) > 1):
                problems.append(f"{name}: thresholds={thresholds}")

    if problems:
        return False, (
            f"ExtProc budget not in effect (expected {expected}): " + "; ".join(problems)
        )
    if not seen:
        return False, "No extproc cluster carries circuit_breakers thresholds yet"

    return f"ExtProc budget {expected} in effect on: {', '.join(sorted(seen))}"


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
