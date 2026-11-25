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

# Controller ceiling (MaxExtProcMaxRequests). Values above it are REJECTED by the
# controller with a silent fallback to the default 8 -- not clamped -- so the range
# is enforced here, before annotating.
MAX_EXTPROC_MAX_REQUESTS = 1024

# PodMonitors making otherwise-unscraped metrics visible to the system
# Prometheus (openshift-ingress is a platform namespace: the system Prometheus
# takes every PodMonitor there, UWM excludes it — see apply_podmonitors).
PODMONITOR_NAME = "praxis-extproc-payload"
GATEWAY_PODMONITOR_NAME = "praxis-gateway-envoy"


def _find_gateway_authpolicy(gateway_name, ingress_namespace):
    """Name of the AuthPolicy owned by the given Gateway (e.g. maas-gateway-auth).

    The policy name is derived from the gateway by maas-controller, not by a
    stable rule, so it is discovered through the ownerReference instead.
    """
    result = oc(
        "get",
        "authpolicy",
        "-n",
        ingress_namespace,
        "-o",
        "json",
        log_stdout=False,  # parsed for ownerReferences only: don't log the JSON blob
    )
    for item in json.loads(result.stdout).get("items", []):
        for owner in item.get("metadata", {}).get("ownerReferences", []):
            if owner.get("kind") == "Gateway" and owner.get("name") == gateway_name:
                return item["metadata"]["name"]
    return None


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
    identity_metrics: bool | None = None,
):
    """Activate or deactivate praxis payload processing on a MaasTenantConfig.

    Both directions annotate maas.opendatahub.io/payload-processing-type explicitly:
    activating sets it to praxis, deactivating opts back into ipp. Praxis is also the
    operator default when the annotation is absent.

    On every toggle (activate and deactivate), the pods of the gateway-side hot-path
    deployments (payload extprocs, gateway, authorino, limitador) are bumped so each
    benchmark endpoint starts from fresh pods.

    On activation, two PodMonitors are created in the ingress namespace so the
    system Prometheus scrapes the payload pods' praxis_extproc_* metrics
    (container port 9090) and the gateway pods' Envoy/kuadrant stats
    (istio-proxy port 15020) — both visible through thanos-querier; they are
    verified (verify_podmonitors) and removed on deactivation.

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
        identity_metrics: Value for the `metrics` field of the identity response filter
            on the gateway AuthPolicy (maas-controller hardcodes true). False removes
            authorino's per-request metric-label evaluation (~38% of its CPU per auth;
            the labels feed usage metering that is broken on streamed /v1/responses
            anyway). True restores it for metering-focused runs. None keeps the
            controller default. Verified against the AuthPolicy and the generated
            AuthConfigs (see verify_identity_metrics).
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

    from projects.ag_praxis.toolbox.capture_maas_praxis.main import (
        EXTPROC_MAX_REQUESTS_ANNOTATION_KEY,
    )

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


@task
def apply_podmonitors(args, ctx):
    """Create the PodMonitors that expose the payload and gateway metrics.

    Two monitors, both in the ingress namespace:

    - praxis-extproc-payload: the payload-pre-processing/payload-processing
      pods (odh-praxis-extproc) serve the praxis_extproc_* metrics on
      container port 9090 ("metrics"), and nothing scrapes them.
    - praxis-gateway-envoy: the gateway pods' istio-proxy serves its merged
      stats on container port 15020 ("metrics", /stats/prometheus) — Envoy
      stats (what Istio's stats_matcher lets through), istio_agent_* and the
      kuadrant wasm-shim counters (kuadrant_allowed/denied/errors — the
      auth-stage outcomes). The platform's own
      maas-default-gateway-metrics PodMonitor scrapes the same port but only
      the istio_* request families survive in Prometheus.

    Both are picked up by the system Prometheus (prometheus-k8s):
    openshift-ingress is a platform namespace, so UWM cannot scrape it (it
    excludes openshift.io/cluster-monitoring-labeled namespaces) but the
    system Prometheus takes every PodMonitor there. The series then reach
    thanos-querier, which is what the kube-burner capture groups
    (config.d/prom.yaml: praxis_extproc, gateway_envoy) query.

    Without them those captures silently return nothing --
    verify_podmonitors fails the run instead if the series never appear.
    Removed on deactivation by delete_podmonitors.
    """
    if not args.activate:
        return "Skipped: praxis is being deactivated"

    import yaml

    from projects.core.dsl.template import render_template
    from projects.core.dsl.utils.k8s import oc_apply

    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    applied = []
    for template_name, context in (
        ("payload-podmonitor.yaml.j2", {"namespace": args.ingress_namespace}),
        (
            "gateway-podmonitor.yaml.j2",
            {"namespace": args.ingress_namespace, "gateway_name": args.gateway_name},
        ),
    ):
        manifest = yaml.safe_load(render_template(template_name, context=context))
        oc_apply(src_dir / template_name.removesuffix(".j2"), manifest)
        applied.append(manifest["metadata"]["name"])

    return f"Applied PodMonitors {', '.join(applied)} in {args.ingress_namespace}"


@task
def delete_podmonitors(args, ctx):
    """Remove the payload and gateway PodMonitors when deactivating praxis."""
    if args.activate:
        return "Skipped: praxis is being activated (PodMonitors stay)"

    for name in (PODMONITOR_NAME, GATEWAY_PODMONITOR_NAME):
        oc(
            "delete",
            "podmonitor",
            name,
            "-n",
            args.ingress_namespace,
            "--ignore-not-found",
        )
    return f"Deleted PodMonitors {PODMONITOR_NAME}, {GATEWAY_PODMONITOR_NAME} from {args.ingress_namespace}"


@task
def set_identity_metrics(args, ctx):
    """Set the identity-metrics flag on the gateway AuthPolicy.

    maas-controller hardcodes `metrics: true` on the identity response filter.
    With it on, authorino re-compiles and re-evaluates the filter's CEL label
    expressions on every request (metricLabels, 72-88% of its CPU in pprof
    profiles); turning it off cut authorino CPU per successful auth by ~38%.
    The labels feed usage metering that is broken on streamed /v1/responses
    anyway (token metering fails open -- Finding 2 of
    maas-praxis-500-ext-proc-overflow.md).

    The patch targets the AuthPolicy owned by the gateway; kuadrant propagates
    it to the generated AuthConfigs (verified by verify_identity_metrics).
    """
    if not args.activate:
        return "Skipped: praxis is being deactivated (policy left as-is)"
    if args.identity_metrics is None:
        return "Skipped: no identity_metrics configured, keeping controller default"

    policy_name = _find_gateway_authpolicy(args.gateway_name, args.ingress_namespace)
    if policy_name is None:
        raise RuntimeError(
            f"No AuthPolicy owned by Gateway/{args.gateway_name} found in "
            f"{args.ingress_namespace} -- cannot set identity metrics"
        )

    from projects.ag_praxis.toolbox.capture_maas_praxis.main import identity_metrics_entries

    policy = oc_get_json("authpolicy", name=policy_name, namespace=args.ingress_namespace)
    entries = identity_metrics_entries(policy.get("spec"))
    if not entries:
        raise RuntimeError(
            f"AuthPolicy {policy_name} has no identity filter with a metrics field "
            "-- policy layout changed?"
        )

    already = all(value is args.identity_metrics for _, value in entries)
    if already:
        return f"identity metrics already {args.identity_metrics} on {policy_name}"

    ops = [{"op": "replace", "path": path, "value": args.identity_metrics} for path, _ in entries]
    oc(
        "patch",
        "authpolicy",
        policy_name,
        "-n",
        args.ingress_namespace,
        "--type",
        "json",
        "-p",
        json.dumps(ops),
    )
    return (
        f"Set identity metrics={args.identity_metrics} on {policy_name} ({len(entries)} filter(s))"
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
def wait_authorino_ready(args, ctx):
    """Wait for the Authorino CR to report Ready after the pod bump."""
    resource = oc_get_json(
        "authorino",
        name="authorino",
        namespace=args.kuadrant_namespace,
        ignore_not_found=True,
    )
    if resource is None:
        return False, "authorino CR not found"

    for condition in resource.get("status", {}).get("conditions", []):
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return "Authorino CR is Ready"

    return False, "authorino CR not Ready yet"


@retry(attempts=60, delay=10, backoff=1.0)
@task
def wait_limitador_ready(args, ctx):
    """Wait for the Limitador CR to report Ready after the pod bump."""
    resource = oc_get_json(
        "limitador",
        name="limitador",
        namespace=args.kuadrant_namespace,
        ignore_not_found=True,
    )
    if resource is None:
        return False, "limitador CR not found"

    for condition in resource.get("status", {}).get("conditions", []):
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return "Limitador CR is Ready"

    return False, "limitador CR not Ready yet"


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
        EXTPROC_VERIFIED_FIELDS,
        extract_extproc_cluster_limits,
        fetch_gateway_config_dump,
    )

    try:
        config_dump = fetch_gateway_config_dump(args.ingress_namespace, args.gateway_name)
    except RuntimeError as exc:
        return False, str(exc)

    limits = extract_extproc_cluster_limits(config_dump)
    if not limits:
        return False, "No extproc clusters in the gateway Envoy config yet"

    expected = args.extproc_max_requests
    problems = []
    seen = []
    for name, data in sorted(limits.items()):
        entries = data["limits"]
        # A threshold_entries count greater than one means a botched MERGE
        # appended to the repeated field -- the original limits may still be
        # in force.
        if not entries:
            if "payload" in name:
                problems.append(f"{name}: no circuit_breakers thresholds")
            continue

        seen.append(name)
        for entry in entries:
            mismatched = {
                field: entry.get(field)
                for field in EXTPROC_VERIFIED_FIELDS
                if entry.get(field) is not None and entry[field] != expected
            }
            if "payload" in name and (mismatched or data["threshold_entries"] > 1):
                problems.append(f"{name}: limits={entries}")

    if problems:
        return False, (
            f"ExtProc budget not in effect (expected {expected}): " + "; ".join(problems)
        )
    if not seen:
        return False, "No extproc cluster carries circuit_breakers thresholds yet"

    return f"ExtProc budget {expected} in effect on: {', '.join(sorted(seen))}"


@retry(attempts=30, delay=10, backoff=1.0)
@task
def verify_identity_metrics(args, ctx):
    """Verify the identity-metrics flag on the AuthPolicy and its generated AuthConfigs.

    Ground truth is both layers: the AuthPolicy itself (maas-controller
    regenerates it from its template on reconciles, e.g. model registration,
    which reverts a manual patch) and the AuthConfigs kuadrant derives from it
    (what authorino actually evaluates). Fails the run if the flag never takes
    effect, instead of silently benchmarking with the expensive per-request
    metric-label evaluation on.
    """
    if not args.activate or args.identity_metrics is None:
        return "Skipped: no identity metrics to verify"

    from projects.ag_praxis.toolbox.capture_maas_praxis.main import identity_metrics_entries

    policy_name = _find_gateway_authpolicy(args.gateway_name, args.ingress_namespace)
    if policy_name is None:
        return False, f"No AuthPolicy owned by Gateway/{args.gateway_name} yet"

    policy = oc_get_json("authpolicy", name=policy_name, namespace=args.ingress_namespace)
    entries = identity_metrics_entries(policy.get("spec"))
    if not entries:
        return False, f"AuthPolicy {policy_name} has no identity filter with a metrics field yet"

    mismatched = [path for path, value in entries if value is not args.identity_metrics]
    if mismatched:
        return (
            False,
            f"AuthPolicy {policy_name} reverted (metrics != {args.identity_metrics}): {mismatched}",
        )

    result = oc(
        "get",
        "authconfig",
        "-n",
        args.kuadrant_namespace,
        "-o",
        "json",
        log_stdout=False,  # parsed for identity filters only: don't log the JSON blob
    )
    total = matching = 0
    for authconfig in json.loads(result.stdout).get("items", []):
        for _, value in identity_metrics_entries(authconfig.get("spec")):
            total += 1
            matching += value is args.identity_metrics

    if total == 0:
        return False, "No AuthConfigs with an identity filter yet (kuadrant lag)"
    if matching < total:
        return (
            False,
            f"AuthConfigs not propagated yet: {matching}/{total} match {args.identity_metrics}",
        )

    return (
        f"identity metrics={args.identity_metrics} on {policy_name} and {total}/{total} AuthConfigs"
    )


def _query_thanos(promql: str) -> list[dict]:
    """Instant-query thanos-querier with a short-lived prometheus-k8s SA token.

    Resolves the querier's route, mints a 10-minute token for the
    prometheus-k8s service account, and curls the query API -- no kube-burner
    capture, no extra pod: the whole probe is two oc calls and one curl.

    A Bearer token is required even though the runner authenticates to the
    API server with client certificates: the thanos query API accepts only
    Bearer auth, and the API server's service proxy does not forward
    credentials (verified live on kpouget-llmd, 2026-11-24: /-/ready answers
    OK through the proxy but /api/v1/query returns 401 with the client-cert
    kubeconfig). The minted token reaches curl on stdin (-H @- reads header
    lines from stdin), so it never appears in command text or logs.

    Raises RuntimeError on any failure so callers decide the policy (the
    verification task retries, missing series are reported separately).
    """
    from projects.core.dsl import shell

    route = oc(
        "get",
        "route",
        "thanos-querier",
        "-n",
        "openshift-monitoring",
        "-o",
        "jsonpath={.spec.host}",
    )
    host = route.stdout.strip()
    if not host:
        raise RuntimeError("Failed to resolve the thanos-querier route")

    token = oc(
        "create",
        "token",
        "prometheus-k8s",
        "-n",
        "openshift-monitoring",
        "--duration=10m",
        log_stdout=False,  # the token is a credential: never log it
    ).stdout.strip()
    if not token:
        raise RuntimeError("Failed to mint a prometheus-k8s service-account token")

    result = shell.run(
        [
            "curl",
            "-sk",
            "--max-time",
            "30",
            "-H",
            "@-",
            f"https://{host}/api/v1/query",
            "--data-urlencode",
            f"query={promql}",
        ],
        shell=False,
        input_text=f"Authorization: Bearer {token}\n",
        timeout_seconds=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"thanos-querier query failed (rc={result.returncode})")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected response from thanos-querier: {exc}") from exc

    if payload.get("status") != "success":
        raise RuntimeError(f"thanos-querier query error: {payload.get('error', payload)}")
    return payload.get("data", {}).get("result", [])


@retry(attempts=8, delay=20, backoff=1.0)
@task
def verify_podmonitors(args, ctx):
    """Verify the PodMonitors' series are being scraped (direct thanos query).

    One instant query per PodMonitor against thanos-querier -- through its
    route, authenticated with a short-lived prometheus-k8s service-account
    token (see _query_thanos) -- requiring one live series per check: the
    raw counters praxis_extproc_requests_total and envoy_server_live.

    Split severity: the gateway-envoy check is strict (it catches wiring
    bugs -- PodMonitor not picked up, target port unreachable -- and an empty
    capture group at the end of the test would look like "no traffic"
    instead of "not scraped"). The praxis_extproc check is advisory: the
    extproc metrics endpoint cannot be scraped by the system Prometheus on
    OCP 4.21 (Finding 6 of maas-praxis-500-ext-proc-overflow.md --
    metrics_auth on the pre-processing pod, blank Content-Type rejected by
    Prometheus 3 on both), so failing the run on it would block every
    campaign until the upstream fixes land. It reports a KNOWN-BROKEN
    warning instead, and the praxis_extproc capture groups stay empty.

    The series are scraped by the system Prometheus (prometheus-k8s), not
    UWM: openshift-ingress is a platform namespace
    (openshift.io/cluster-monitoring label), which the system Prometheus
    requires and UWM excludes; the payload-processing NetworkPolicy
    explicitly allows the openshift-monitoring namespace to port 9090. The
    retry window covers the prometheus-operator picking up the PodMonitors,
    the config reload and the first scrapes.
    """
    if not args.activate:
        return "Skipped: praxis is being deactivated"

    # (promql, description, strict)
    checks = [
        (
            f'count(praxis_extproc_requests_total{{namespace="{args.ingress_namespace}"}})',
            f"praxis_extproc metrics (PodMonitor {PODMONITOR_NAME})",
            False,
        ),
        (
            f'count(envoy_server_live{{namespace="{args.ingress_namespace}", container="istio-proxy"}})',
            f"gateway envoy stats (PodMonitor {GATEWAY_PODMONITOR_NAME})",
            True,
        ),
    ]

    missing_strict = []
    missing_advisory = []
    for promql, description, strict in checks:
        try:
            results = _query_thanos(promql)
        except RuntimeError as exc:
            return False, f"Cannot query thanos-querier: {exc}"
        if not results:
            (missing_strict if strict else missing_advisory).append(description)

    if missing_strict:
        return False, (
            "Series not visible in thanos-querier: "
            + "; ".join(missing_strict)
            + " (PodMonitor not picked up by the system Prometheus, or the "
            "target port unreachable)"
        )

    if missing_advisory:
        return (
            "PodMonitors scraped and visible (gateway envoy). "
            "KNOWN-BROKEN (advisory): "
            + "; ".join(missing_advisory)
            + " -- the extproc metrics endpoint cannot be scraped by the "
            "system Prometheus on this cluster (Finding 6 of "
            "maas-praxis-500-ext-proc-overflow.md: metrics_auth on the "
            "pre-processing pod, blank Content-Type rejected by Prometheus 3 "
            "on both); the praxis_extproc capture groups will be empty until "
            "the upstream fix."
        )

    return "PodMonitors scraped and visible: " + ", ".join(d for _, d, _ in checks)


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
