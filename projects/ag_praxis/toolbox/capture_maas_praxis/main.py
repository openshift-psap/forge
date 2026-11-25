from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc, oc_get_json

logger = logging.getLogger("DSL")

GATEWAY_NAME_LABEL = "gateway.networking.k8s.io/gateway-name"

# ExtProc concurrency budget: the annotation read by ai-gateway-controller, the
# fields the controller writes into the Envoy circuit breakers, and the operator
# default applied when the annotation is absent or out of range (silently -- not
# clamped). Shared with enable_maas_praxis, which sets and verifies the budget.
EXTPROC_MAX_REQUESTS_ANNOTATION_KEY = "maas.opendatahub.io/experimental-extproc-max-requests"
OPERATOR_DEFAULT_EXTPROC_MAX_REQUESTS = 8
EXTPROC_CIRCUIT_BREAKER_FIELDS = (
    "max_requests",
    "max_pending_requests",
    "max_connections",
    "max_concurrent_streams",
)
# Fields verified against the annotated budget (the controller is expected to
# write the same value into all four, but only these three existed when the
# verification was written -- kept as-is to avoid flagging pre-existing configs).
EXTPROC_VERIFIED_FIELDS = ("max_requests", "max_pending_requests", "max_connections")


@dataclass(frozen=True)
class DeploymentTarget:
    """A deployment to capture: name, namespace, pod selector and dedicated artifact directory."""

    name: str
    namespace: str
    selector: str
    dirname: str

    def artifact_dir(self, artifacts_root: Path) -> Path:
        """Return the dedicated artifact directory of this component."""
        return artifacts_root / self.dirname


def _capture(description: str, path, *command: str) -> str:
    try:
        result = oc(*command, check=False, log_stdout=False, log_stderr=False, stdout_dest=path)
    except Exception as exc:
        logger.warning("Failed to capture %s: %s", description, exc)
        return f"Failed to capture {description}"
    if result.returncode:
        logger.warning(
            "Failed to capture %s (oc exited with status %d)", description, result.returncode
        )
        return f"Failed to capture {description}"
    return f"Captured {description} to {path}"


def _resolve_gateway_deployment_name(gateway_name: str, namespace: str) -> str:
    """Resolve the deployment backing a Gateway (name is '{gateway}-{gatewayclass}')."""
    deployments = oc_get_json(
        "deployment",
        selector=f"{GATEWAY_NAME_LABEL}={gateway_name}",
        namespace=namespace,
        ignore_not_found=True,
    )
    items = (deployments or {}).get("items", [])
    if not items:
        logger.warning(
            "No deployment found for gateway %s in namespace %s, falling back to its name",
            gateway_name,
            namespace,
        )
        return gateway_name
    return items[0]["metadata"]["name"]


@entrypoint
def run(
    *,
    ingress_namespace: str = "openshift-ingress",
    tenant_namespace: str = "models-as-a-service",
    tenant_name: str = "default-tenant",
    kuadrant_namespace: str = "kuadrant-system",
    gateway_name: str = "maas-default-gateway",
):
    """Capture the state of the inference hot-path deployments for debugging.

    Covers the payload-processing deployments, the MaaS gateway, and the kuadrant
    auth/ratelimiting backends (authorino, limitador), plus the MaasTenantConfig,
    the AuthPolicies/AuthConfigs, and the effective ExtProc circuit-breaker limits
    from the gateway's live Envoy config. The final task condenses the observed
    praxis configuration into artifacts/praxis_state_summary.json (ext_proc
    budget, identity-metrics flag, authorino settings) -- the input of the
    500-cliff postprocess report.

    Each component's files are saved in a dedicated directory under the artifacts
    directory (e.g. artifacts/authorino/deployment.yaml), with its pod logs under
    a logs/ subdirectory.

    Args:
        ingress_namespace: Namespace where the gateway and payload-processing deployments live.
        tenant_namespace: Namespace of the MaasTenantConfig resource.
        tenant_name: Name of the MaasTenantConfig resource.
        kuadrant_namespace: Namespace where the authorino and limitador deployments live.
        gateway_name: Name of the MaaS Gateway resource.
    """
    return execute_tasks(locals())


def build_hot_path_targets(
    ingress_namespace: str, kuadrant_namespace: str, gateway_name: str
) -> list[DeploymentTarget]:
    """Build the list of inference hot-path deployments (payload extprocs, gateway, kuadrant backends)."""
    return [
        DeploymentTarget(
            name="payload-processing",
            namespace=ingress_namespace,
            selector="app=payload-processing",
            dirname="payload-processing",
        ),
        DeploymentTarget(
            name="payload-pre-processing",
            namespace=ingress_namespace,
            selector="app=payload-pre-processing",
            dirname="payload-pre-processing",
        ),
        DeploymentTarget(
            name=_resolve_gateway_deployment_name(gateway_name, ingress_namespace),
            namespace=ingress_namespace,
            selector=f"{GATEWAY_NAME_LABEL}={gateway_name}",
            dirname="gateway",
        ),
        DeploymentTarget(
            name="authorino",
            namespace=kuadrant_namespace,
            selector="authorino-resource=authorino",
            dirname="authorino",
        ),
        DeploymentTarget(
            name="limitador-limitador",
            namespace=kuadrant_namespace,
            selector="app=limitador",
            dirname="limitador",
        ),
    ]


@task
def resolve_hot_path_deployments(args, ctx):
    """Resolve the hot-path deployments to capture (gateway deployment name is discovered by label)."""
    ctx.hot_path_deployments = build_hot_path_targets(
        args.ingress_namespace, args.kuadrant_namespace, args.gateway_name
    )
    names = ", ".join(f"{t.name} ({t.namespace})" for t in ctx.hot_path_deployments)
    return f"Hot-path deployments to capture: {names}"


@task
def capture_overview(args, ctx):
    """Capture wide listing of pods and deployments in the ingress and kuadrant namespaces."""
    artifacts = args.artifact_dir / "artifacts"

    namespaces = {
        "ingress": args.ingress_namespace,
        "kuadrant": args.kuadrant_namespace,
    }
    results = []
    for dirname, namespace in namespaces.items():
        namespace_dir = artifacts / dirname
        namespace_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"deployments wide listing ({namespace})",
                namespace_dir / "deployments_wide.txt",
                "get",
                "deployments",
                "-n",
                namespace,
                "-owide",
            )
        )
        results.append(
            _capture(
                f"pods wide listing ({namespace})",
                namespace_dir / "pods_wide.txt",
                "get",
                "pods",
                "-n",
                namespace,
                "-owide",
            )
        )
    return "; ".join(results)


@task
def capture_deployments(args, ctx):
    """Capture YAML, wide listing and describe output for each hot-path deployment."""
    artifacts = args.artifact_dir / "artifacts"

    results = []
    for target in ctx.hot_path_deployments:
        target_dir = target.artifact_dir(artifacts)
        target_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"{target.name} yaml",
                target_dir / "deployment.yaml",
                "get",
                "deployment",
                target.name,
                "-n",
                target.namespace,
                "-o",
                "yaml",
            )
        )
        results.append(
            _capture(
                f"{target.name} wide listing",
                target_dir / "deployment_wide.txt",
                "get",
                "deployment",
                target.name,
                "-n",
                target.namespace,
                "-owide",
            )
        )
        results.append(
            _capture(
                f"{target.name} describe",
                target_dir / "deployment_describe.txt",
                "describe",
                "deployment",
                target.name,
                "-n",
                target.namespace,
            )
        )
    return "; ".join(results)


@task
def capture_pods(args, ctx):
    """Capture YAML, status and describe output for the hot-path deployment pods."""
    artifacts = args.artifact_dir / "artifacts"

    results = []
    for target in ctx.hot_path_deployments:
        target_dir = target.artifact_dir(artifacts)
        target_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"{target.name} pods yaml",
                target_dir / "pods.yaml",
                "get",
                "pods",
                "-l",
                target.selector,
                "-n",
                target.namespace,
                "-o",
                "yaml",
            )
        )
        results.append(
            _capture(
                f"{target.name} pods status",
                target_dir / "pods.txt",
                "get",
                "pods",
                "-l",
                target.selector,
                "-n",
                target.namespace,
                "-owide",
            )
        )
        results.append(
            _capture(
                f"{target.name} pods describe",
                target_dir / "pods_describe.txt",
                "describe",
                "pods",
                "-l",
                target.selector,
                "-n",
                target.namespace,
            )
        )
    return "; ".join(results)


@task
def capture_pod_logs(args, ctx):
    """Capture logs for the hot-path deployment pods.

    `--tail=-1` is mandatory: `oc logs` defaults to only the last 10 lines when a
    selector is given, which would truncate the gateway's Envoy access log — the
    record of every request the gateway rejected, and the only place where the
    failing filter is named (`%RESPONSE_CODE_DETAILS%`).

    No tail cap is needed otherwise: the pods are bumped fresh before each benchmark
    endpoint, so the logs are bounded by one benchmark window.
    """
    artifacts = args.artifact_dir / "artifacts"

    results = []
    for target in ctx.hot_path_deployments:
        logs_dir = target.artifact_dir(artifacts) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            _capture(
                f"{target.name} logs",
                logs_dir / "pods.log",
                "logs",
                "-l",
                target.selector,
                "-n",
                target.namespace,
                "--all-containers",
                "--prefix",
                "--tail=-1",
            )
        )
        results.append(
            _capture(
                f"{target.name} previous logs",
                logs_dir / "pods_previous.log",
                "logs",
                "-l",
                target.selector,
                "-n",
                target.namespace,
                "--all-containers",
                "--prefix",
                "--previous",
                "--tail=-1",
            )
        )
    return "; ".join(results)


@task
def capture_maas_tenant_config(args, ctx):
    """Capture the MaasTenantConfig resource."""
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    results = []
    results.append(
        _capture(
            "MaasTenantConfig yaml",
            artifacts / "maastenantconfig.yaml",
            "get",
            "maastenantconfig",
            args.tenant_name,
            "-n",
            args.tenant_namespace,
            "-o",
            "yaml",
        )
    )
    results.append(
        _capture(
            "MaasTenantConfig describe",
            artifacts / "maastenantconfig_describe.txt",
            "describe",
            "maastenantconfig",
            args.tenant_name,
            "-n",
            args.tenant_namespace,
        )
    )
    return "; ".join(results)


@task
def capture_auth_policies(args, ctx):
    """Capture the gateway AuthPolicies, including the identity-metrics value.

    The AuthPolicy yaml records whether the identity response filter had its
    metric-label evaluation on or off during the run (platform.praxis.
    identity_metrics / enable_maas_praxis) -- without it, runs are not
    comparable. The summary file extracts just that flag; the yaml is the
    robust record (no credentials in AuthPolicies, safe for artifacts).
    """
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    results = [
        _capture(
            "AuthPolicies yaml (includes the identity-metrics flag)",
            artifacts / "authpolicies.yaml",
            "get",
            "authpolicy",
            "-n",
            args.ingress_namespace,
            "-o",
            "yaml",
        ),
        _capture(
            "AuthPolicy identity-metrics summary",
            artifacts / "authpolicy_identity_metrics.txt",
            "get",
            "authpolicy",
            "-n",
            args.ingress_namespace,
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}: "
            'identity.metrics={.spec.defaults.rules.response.success.filters.identity.metrics}{"\\n"}{end}',
        ),
    ]
    return "; ".join(results)


@task
def capture_kuadrant_crs(args, ctx):
    """Capture the Authorino and Limitador CRs from the kuadrant namespace."""
    artifacts = args.artifact_dir / "artifacts" / "kuadrant"
    artifacts.mkdir(parents=True, exist_ok=True)

    results = []
    for kind in ("authorino", "limitador"):
        results.append(
            _capture(
                f"{kind} CR yaml",
                artifacts / f"{kind}.yaml",
                "get",
                kind,
                kind,
                "-n",
                args.kuadrant_namespace,
                "-o",
                "yaml",
            )
        )
        results.append(
            _capture(
                f"{kind} CR describe",
                artifacts / f"{kind}_describe.txt",
                "describe",
                kind,
                kind,
                "-n",
                args.kuadrant_namespace,
            )
        )
    return "; ".join(results)


@task
def capture_extproc_limits(args, ctx):
    """Capture the effective ExtProc circuit-breaker limits from the live gateway.

    Ground truth for the ext_proc concurrency budget is the gateway pod's Envoy
    config_dump, not the CRs: out-of-range annotation values silently fall back
    to the operator default, and reconciles can revert patches. Only the
    extracted circuit-breaker section is persisted -- never the full config_dump.
    """
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    try:
        config_dump = fetch_gateway_config_dump(args.ingress_namespace, args.gateway_name)
    except RuntimeError as exc:
        logger.warning("Failed to fetch the gateway config_dump: %s", exc)
        return f"Failed to capture ext_proc limits: {exc}"

    limits = extract_extproc_cluster_limits(config_dump)
    if not limits:
        return "No ext_proc clusters in the gateway Envoy config (praxis not active?)"

    dest = artifacts / "extproc_limits.json"
    dest.write_text(json.dumps(limits, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return f"Captured ext_proc circuit-breaker limits of {len(limits)} cluster(s) to {dest}"


@task
def capture_authconfigs(args, ctx):
    """Capture the generated AuthConfigs (what authorino actually evaluates)."""
    artifacts = args.artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    return _capture(
        "AuthConfigs yaml (kuadrant-generated, evaluated by authorino)",
        artifacts / "authconfigs.yaml",
        "get",
        "authconfig",
        "-n",
        args.kuadrant_namespace,
        "-o",
        "yaml",
    )


@task
def write_state_summary(args, ctx):
    """Write praxis_state_summary.json: the observed praxis config of this capture.

    Everything is derived from the files captured by the earlier tasks of this
    command, so the summary is reproducible post-run and reports can state what
    the cluster actually ran with -- not what the harness configuration asked
    for. Missing inputs are recorded as null, never guessed.
    """
    artifacts = args.artifact_dir / "artifacts"
    summary = {
        "extproc_max_requests": summarize_extproc_budget(artifacts),
        "identity_metrics": summarize_identity_metrics(artifacts),
        "authorino": summarize_authorino(artifacts),
    }
    dest = artifacts / "praxis_state_summary.json"
    dest.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return f"Wrote praxis state summary to {dest}"


def fetch_gateway_config_dump(ingress_namespace: str, gateway_name: str) -> dict:
    """Fetch the gateway pod's dynamic active clusters from the Envoy admin API.

    Raises RuntimeError on failure so callers decide the policy: verification
    tasks retry, capture tasks record the gap.
    """
    deployment = _resolve_gateway_deployment_name(gateway_name, ingress_namespace)
    result = oc(
        "exec",
        "-n",
        ingress_namespace,
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
        raise RuntimeError(
            f"config_dump fetch failed on deploy/{deployment}: rc={result.returncode}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"config_dump from deploy/{deployment} is not valid JSON: {exc}"
        ) from exc


def extract_extproc_cluster_limits(config_dump: dict) -> dict[str, dict]:
    """Circuit-breaker limits of the ext_proc clusters in an Envoy config_dump.

    Returns {cluster_name: {"threshold_entries": int, "limits": [{field: value}]}}.
    A threshold_entries greater than one means a botched MERGE appended to the
    repeated field -- the original limits may still be in force.
    """
    limits = {}
    for config_entry in config_dump.get("configs", []):
        cluster = config_entry.get("cluster", {}) if isinstance(config_entry, dict) else {}
        name = cluster.get("name", "")
        if "extproc" not in name:
            continue
        thresholds = cluster.get("circuit_breakers", {}).get("thresholds") or []
        limits[name] = {
            "threshold_entries": len(thresholds),
            "limits": [
                {field: entry.get(field) for field in EXTPROC_CIRCUIT_BREAKER_FIELDS}
                for entry in thresholds
            ],
        }
    return limits


def identity_metrics_entries(spec) -> list[tuple[str, bool]]:
    """(json_patch_path, current_value) for identity response filters with a metrics field.

    Walks the whole spec so any AuthPolicy/AuthConfig layout works (rules under
    spec or under defaults/overrides). Shared with enable_maas_praxis, which
    patches and verifies the flag.
    """
    entries = []

    def walk(node, path):
        if isinstance(node, dict):
            if path and path[-1] == "identity" and "json" in node and "metrics" in node:
                entries.append(
                    ("/spec/" + "/".join(str(p) for p in path) + "/metrics", node["metrics"])
                )
                return
            for k, v in node.items():
                walk(v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [i])

    walk(spec or {}, [])
    return entries


def _load_yaml_file(path: Path):
    """Load a YAML artifact captured by this command; None when absent/unparseable.

    A capture gap must surface as null in the summary (visible in the reports),
    not crash the whole capture -- hence warn-and-None rather than raising.
    """
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse captured artifact %s: %s", path, exc)
        return None


def summarize_extproc_budget(artifacts: Path) -> dict:
    """Observed ExtProc budget: annotated value vs effective Envoy circuit breakers."""
    tenant = _load_yaml_file(artifacts / "maastenantconfig.yaml") or {}
    annotations = tenant.get("metadata", {}).get("annotations") or {}
    raw = annotations.get(EXTPROC_MAX_REQUESTS_ANNOTATION_KEY)

    annotation = None
    annotation_invalid = None
    if raw is not None:
        try:
            annotation = int(raw)
        except (TypeError, ValueError):
            annotation_invalid = str(raw)

    effective = None
    limits_path = artifacts / "extproc_limits.json"
    if limits_path.exists():
        try:
            effective = json.loads(limits_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse %s: %s", limits_path, exc)

    consistent = None
    if effective:
        payload_values = {
            entry.get("max_requests")
            for name, data in effective.items()
            if "payload" in name
            for entry in data.get("limits", [])
        }
        expected = annotation if annotation is not None else OPERATOR_DEFAULT_EXTPROC_MAX_REQUESTS
        if payload_values:
            consistent = payload_values == {expected}

    summary = {"annotation": annotation, "effective": effective, "consistent": consistent}
    if annotation_invalid is not None:
        summary["annotation_invalid"] = annotation_invalid
    return summary


def summarize_identity_metrics(artifacts: Path) -> dict:
    """Observed identity-metrics flag: AuthPolicy layer and generated AuthConfig layer."""
    policies = {}
    for item in (_load_yaml_file(artifacts / "authpolicies.yaml") or {}).get("items", []):
        name = item.get("metadata", {}).get("name", "?")
        entries = identity_metrics_entries(item.get("spec"))
        if entries:
            policies[name] = {"identity_metrics": entries[0][1], "filters": len(entries)}

    authconfigs = None
    authconfigs_doc = _load_yaml_file(artifacts / "authconfigs.yaml")
    if authconfigs_doc is not None:
        total = 0
        values = set()
        for item in authconfigs_doc.get("items", []):
            for _, value in identity_metrics_entries(item.get("spec")):
                total += 1
                values.add(value)
        authconfigs = {
            "total_filters": total,
            "identity_metrics": values.pop() if len(values) == 1 else None,
        }

    return {"policies": policies, "authconfigs": authconfigs}


def summarize_authorino(artifacts: Path) -> dict | None:
    """Observed authorino CR settings (replicas, evaluator cache size)."""
    doc = _load_yaml_file(artifacts / "kuadrant" / "authorino.yaml")
    if not doc:
        return None
    spec = doc.get("spec") or {}
    return {
        "replicas": spec.get("replicas"),
        "evaluator_cache_size": spec.get("evaluatorCacheSize"),
    }


if __name__ == "__main__":
    run.main()
