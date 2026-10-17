#!/usr/bin/env python3

from __future__ import annotations

import logging

import yaml

from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import best_effort_oc, oc, oc_apply

logger = logging.getLogger("DSL")

_VALID_MODEL_REF_KINDS = ("LLMInferenceService", "ExternalModel")


@entrypoint
def run(
    *,
    model_namespace: str,
    tenant_namespace: str,
    llmisvc_name: str,
    model_ref_name: str = "",
    model_ref_kind: str = "LLMInferenceService",
    external_endpoint: str = "",
    external_target_model: str = "",
    external_service_name: str = "",
    external_port: int = 8080,
    external_path: str = "/v1",
    gateway_name: str = "",
    gateway_namespace: str = "",
    owner_deployment_name: str = "",
):
    """Register a model with MaaS by creating MaaSModelRef, MaaSAuthPolicy, and MaaSSubscription.

    For ExternalModel kind, also creates credential secret, ExternalProvider,
    ExternalModel (both maas and inference CRDs).

    Args:
        model_namespace: Namespace where the LLMInferenceService or ExternalModel lives.
        tenant_namespace: Namespace for MaaSAuthPolicy and MaaSSubscription.
        llmisvc_name: Name of the LLMInferenceService to reference (or ExternalModel name).
        model_ref_name: Name for the MaaSModelRef resource. Defaults to llmisvc_name.
        model_ref_kind: Kind of resource the MaaSModelRef references ("LLMInferenceService" or "ExternalModel").
        external_endpoint: Service endpoint for ExternalModel (e.g. "praxis.ns.svc.cluster.local").
        external_target_model: Target model name for ExternalModel.
        external_service_name: K8s Service name for the backend (e.g. "praxis").
        external_port: Port for the backend service.
        external_path: Path prefix for IPP path rewriting (e.g. "/v1").
        gateway_name: MaaS gateway name (required for ExternalModel).
        gateway_namespace: MaaS gateway namespace (required for ExternalModel).
        owner_deployment_name: Name of the Deployment that owns the created resources (for garbage collection).
    """
    if model_ref_kind not in _VALID_MODEL_REF_KINDS:
        raise ValueError(
            f"Invalid model_ref_kind: {model_ref_kind!r}. Valid: {_VALID_MODEL_REF_KINDS}"
        )
    if model_ref_kind == "ExternalModel" and not external_endpoint:
        raise ValueError("external_endpoint is required when model_ref_kind is ExternalModel")
    if model_ref_kind == "ExternalModel" and not external_service_name:
        raise ValueError("external_service_name is required when model_ref_kind is ExternalModel")

    return execute_tasks(locals())


@task
def resolve_names(args, ctx):
    """Resolve default names and fetch the LLMInferenceService UID (if applicable)."""
    ctx.model_ref_name = args.model_ref_name or args.llmisvc_name
    ctx.is_external = args.model_ref_kind == "ExternalModel"
    ctx.created_model_ref = False
    ctx.created_auth_policy = False
    ctx.created_subscription = False
    ctx.created_external_model = False
    ctx.created_inference_external_model = False
    ctx.created_external_provider = False

    if ctx.is_external:
        ctx.llmisvc_uid = None
    else:
        ctx.llmisvc_uid = oc(
            "get",
            "llminferenceservice",
            args.llmisvc_name,
            "-n",
            args.model_namespace,
            "-o",
            "jsonpath={.metadata.uid}",
            log_stdout=True,
        ).stdout.strip()

    ctx.owner_ref = None
    if args.owner_deployment_name:
        owner_uid = oc(
            "get",
            "deployment",
            args.owner_deployment_name,
            "-n",
            args.model_namespace,
            "-o",
            "jsonpath={.metadata.uid}",
            log_stdout=True,
        ).stdout.strip()
        ctx.owner_ref = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": args.owner_deployment_name,
            "uid": owner_uid,
        }

    return f"Model ref name: {ctx.model_ref_name}, kind: {args.model_ref_kind}"


@task
def delete_previous(args, ctx):
    """Delete any existing MaaS resources before re-creating them."""
    best_effort_oc(
        "delete",
        "maassubscription",
        f"{args.model_namespace}-{ctx.model_ref_name}-subscription",
        "-n",
        args.tenant_namespace,
    )
    best_effort_oc(
        "delete",
        "maasauthpolicy",
        f"{args.model_namespace}-{ctx.model_ref_name}-access",
        "-n",
        args.tenant_namespace,
    )
    best_effort_oc("delete", "maasmodelref", ctx.model_ref_name, "-n", args.model_namespace)

    if ctx.is_external:
        best_effort_oc(
            "delete",
            "externalmodel.inference.opendatahub.io",
            ctx.model_ref_name,
            "-n",
            args.model_namespace,
        )
        best_effort_oc(
            "delete",
            "externalprovider.inference.opendatahub.io",
            f"{ctx.model_ref_name}-provider",
            "-n",
            args.model_namespace,
        )
        best_effort_oc(
            "delete",
            "externalmodel.maas.opendatahub.io",
            ctx.model_ref_name,
            "-n",
            args.model_namespace,
        )
        best_effort_oc(
            "delete", "httproute", f"{ctx.model_ref_name}-route", "-n", args.model_namespace
        )
        best_effort_oc(
            "delete", "secret", f"{ctx.model_ref_name}-creds", "-n", args.model_namespace
        )

    return "Deleted previous MaaS resources"


def _template_context(args, ctx):
    return {
        "model_namespace": args.model_namespace,
        "tenant_namespace": args.tenant_namespace,
        "llmisvc_name": args.llmisvc_name,
        "model_ref_name": ctx.model_ref_name,
        "model_ref_kind": args.model_ref_kind,
        "is_external": ctx.is_external,
        "llmisvc_uid": ctx.llmisvc_uid,
        "external_endpoint": args.external_endpoint,
        "external_target_model": args.external_target_model,
        "external_service_name": args.external_service_name,
        "external_port": args.external_port,
        "external_path": args.external_path,
        "gateway_name": args.gateway_name,
        "gateway_namespace": args.gateway_namespace,
        "owner_ref": ctx.owner_ref,
    }


@task
def apply_external_model(args, ctx):
    """Create credential secret, ExternalProvider, and ExternalModel CRs (only for ExternalModel kind).

    Creates:
    - Credential secret for the ExternalProvider auth
    - maas.opendatahub.io ExternalModel (triggers operator to create routing infrastructure)
    - inference.opendatahub.io ExternalProvider (backend endpoint for IPP)
    - inference.opendatahub.io ExternalModel (path rewriting for IPP)
    """
    if not ctx.is_external:
        return "Skipped (not ExternalModel)"

    import base64

    secret_name = f"{ctx.model_ref_name}-creds"
    dummy_key = base64.b64encode(f"dummy-key-{ctx.model_ref_name}".encode()).decode()
    secret_metadata = {
        "name": secret_name,
        "namespace": args.model_namespace,
        "labels": {
            "inference.networking.k8s.io/bbr-managed": "true",
            "app.kubernetes.io/managed-by": "forge",
        },
    }
    if ctx.owner_ref:
        secret_metadata["ownerReferences"] = [ctx.owner_ref]

    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": secret_metadata,
        "data": {
            "api-key": dummy_key,
        },
    }
    oc("apply", "-f", "-", input_text=yaml.safe_dump(secret_manifest, sort_keys=False))

    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    tctx = _template_context(args, ctx)

    maas_manifest = yaml.safe_load(render_template("external_model.yaml.j2", context=tctx))
    oc_apply(src_dir / "external-model-maas.yaml", maas_manifest)
    ctx.created_external_model = True

    provider_manifest = yaml.safe_load(render_template("external_provider.yaml.j2", context=tctx))
    oc_apply(src_dir / "external-provider.yaml", provider_manifest)
    ctx.created_external_provider = True

    inference_manifest = yaml.safe_load(
        render_template("inference_external_model.yaml.j2", context=tctx)
    )
    oc_apply(src_dir / "external-model-inference.yaml", inference_manifest)
    ctx.created_inference_external_model = True

    return (
        f"Applied ExternalModel + ExternalProvider {ctx.model_ref_name} → {args.external_endpoint}"
    )


@task
def apply_maas_model_ref(args, ctx):
    """Create the MaaSModelRef in the model namespace."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    tctx = _template_context(args, ctx)
    manifest = yaml.safe_load(render_template("maas_model_ref.yaml.j2", context=tctx))
    oc_apply(src_dir / "maas-model-ref.yaml", manifest)
    ctx.created_model_ref = True

    return f"Applied MaaSModelRef {ctx.model_ref_name} (kind={args.model_ref_kind})"


@task
def apply_maas_auth_policy(args, ctx):
    """Create the MaaSAuthPolicy in the tenant namespace."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    tctx = _template_context(args, ctx)
    manifest = yaml.safe_load(render_template("maas_auth_policy.yaml.j2", context=tctx))
    oc_apply(src_dir / "maas-auth-policy.yaml", manifest)
    ctx.created_auth_policy = True

    return f"Applied MaaSAuthPolicy {ctx.model_ref_name}-access"


@task
def apply_maas_subscription(args, ctx):
    """Create the MaaSSubscription in the tenant namespace."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    tctx = _template_context(args, ctx)
    manifest = yaml.safe_load(render_template("maas_subscription.yaml.j2", context=tctx))
    oc_apply(src_dir / "maas-subscription.yaml", manifest)
    ctx.created_subscription = True

    return f"Applied MaaSSubscription {ctx.model_ref_name}-subscription"


@retry(attempts=30, delay=4, backoff=1.0)
@task
def wait_maas_model_ref_ready(args, ctx):
    """Wait for the MaaSModelRef to reach Ready phase."""
    result = oc(
        "get",
        "maasmodelref",
        ctx.model_ref_name,
        "-n",
        args.model_namespace,
        "-o",
        "jsonpath={.status.phase}",
        log_stdout=True,
    )
    phase = result.stdout.strip()
    if phase != "Ready":
        return False, f"MaaSModelRef phase={phase!r}, waiting for Ready"

    return f"MaaSModelRef {ctx.model_ref_name} is Ready"


@retry(attempts=30, delay=4, backoff=1.0)
@task
def patch_operator_resources(args, ctx):
    """Patch operator-created resources for internal HTTP backends.

    The inference.opendatahub.io ExternalProvider controller creates resources
    (Service, ServiceEntry, DestinationRule, HTTPRoute) configured for external
    HTTPS providers (port 443, TLS). For internal HTTP backends, we patch them
    to use the correct port/protocol and add URL rewriting.
    """
    if not ctx.is_external:
        return "Skipped (not ExternalModel)"

    import json

    provider_name = f"{ctx.model_ref_name}-provider"
    path_prefix = f"/{args.model_namespace}/{ctx.model_ref_name}"

    result = oc(
        "get",
        "httproute",
        ctx.model_ref_name,
        "-n",
        args.model_namespace,
        "-o",
        "jsonpath={.metadata.uid}",
        check=False,
        log_stdout=True,
    )
    if not result.stdout.strip():
        return False, "Waiting for IPP-created HTTPRoute"

    oc(
        "annotate",
        "externalmodel.inference.opendatahub.io",
        ctx.model_ref_name,
        "-n",
        args.model_namespace,
        "opendatahub.io/managed=false",
        "--overwrite",
    )

    for route_name in (ctx.model_ref_name, f"maas-{ctx.model_ref_name}"):
        result = oc(
            "get",
            "httproute",
            route_name,
            "-n",
            args.model_namespace,
            "-o",
            "json",
            check=False,
            log_stdout=False,
        )
        if not result.stdout.strip() or result.returncode != 0:
            continue

        route = json.loads(result.stdout)
        for rule in route["spec"].get("rules", []):
            rule["backendRefs"] = [
                {
                    "name": args.external_service_name,
                    "port": args.external_port,
                    "kind": "Service",
                    "group": "",
                }
            ]
            has_path_prefix = any(
                m.get("path", {}).get("value", "").startswith(path_prefix)
                for m in rule.get("matches", [])
            )
            filter_types = [f["type"] for f in rule.get("filters", [])]
            if has_path_prefix and "URLRewrite" not in filter_types:
                rule.setdefault("filters", []).append(
                    {
                        "type": "URLRewrite",
                        "urlRewrite": {
                            "path": {
                                "type": "ReplacePrefixMatch",
                                "replacePrefixMatch": "/",
                            }
                        },
                    }
                )
        oc("replace", "-f", "-", input_text=json.dumps(route))

    oc(
        "patch",
        "destinationrule",
        provider_name,
        "-n",
        args.model_namespace,
        "--type=merge",
        "-p",
        '{"spec":{"trafficPolicy":{"tls":{"mode":"DISABLE"}}}}',
        check=False,
    )

    port_patch = json.dumps(
        {
            "spec": {
                "ports": [
                    {
                        "name": "http",
                        "number": args.external_port,
                        "protocol": "HTTP",
                    }
                ]
            }
        }
    )
    oc(
        "patch",
        "serviceentry",
        provider_name,
        "-n",
        args.model_namespace,
        "--type=merge",
        "-p",
        port_patch,
        check=False,
    )

    svc_patch = json.dumps(
        {
            "spec": {
                "ports": [
                    {
                        "port": args.external_port,
                        "protocol": "TCP",
                        "targetPort": args.external_port,
                    }
                ]
            }
        }
    )
    oc(
        "patch",
        "svc",
        provider_name,
        "-n",
        args.model_namespace,
        "--type=merge",
        "-p",
        svc_patch,
        check=False,
    )

    return f"Patched operator resources for {ctx.model_ref_name}: HTTP/{args.external_port}"


@always
@task
def capture_maas_resources(args, ctx):
    """Capture the YAML of created MaaS resources for post-mortem analysis."""
    captured = []

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if ctx.created_external_model:
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
        captured.append("ExternalModel(maas)")

    if ctx.created_external_provider:
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
        captured.append("ExternalProvider")

    if ctx.created_inference_external_model:
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
        captured.append("ExternalModel(inference)")

    if ctx.created_model_ref:
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
        captured.append("MaaSModelRef")

    if ctx.created_auth_policy:
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
        captured.append("MaaSAuthPolicy")

    if ctx.created_subscription:
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
        captured.append("MaaSSubscription")

    if not captured:
        return "No MaaS resources were created, nothing to capture"

    return f"Captured {', '.join(captured)} for {ctx.model_ref_name}"


if __name__ == "__main__":
    run.main()
