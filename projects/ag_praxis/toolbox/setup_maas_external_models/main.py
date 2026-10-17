from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc, oc_apply

logger = logging.getLogger("DSL")


@entrypoint
def run(
    *,
    namespace: str,
    gateway_name: str,
    gateway_namespace: str,
    sim_service_name: str,
    sim_port: int = 8000,
    model_names: list[str],
):
    """Create ExternalModel CRs, HTTPRoutes, and dummy credential secrets for MaaS benchmarking.

    Args:
        namespace: Namespace where the sim and resources live.
        gateway_name: Name of the MaaS gateway.
        gateway_namespace: Namespace of the MaaS gateway.
        sim_service_name: Name of the sim Service to route to.
        sim_port: Port of the sim Service.
        model_names: List of model names to register.
    """

    if not model_names:
        raise ValueError("model_names must not be empty")

    return execute_tasks(locals())


@task
def create_credential_secrets(args, ctx):
    """Create dummy credential secrets for each model."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    import base64

    for model_name in args.model_names:
        secret_name = f"{model_name}-creds"
        dummy_key = base64.b64encode(f"dummy-key-{model_name}".encode()).decode()

        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": secret_name,
                "namespace": args.namespace,
                "labels": {
                    "inference.networking.k8s.io/bbr-managed": "true",
                    "app.kubernetes.io/managed-by": "forge",
                },
            },
            "data": {
                "api-key": dummy_key,
            },
        }

        import yaml

        oc("apply", "-f", "-", input_text=yaml.safe_dump(manifest, sort_keys=False))

    return f"Created {len(args.model_names)} credential secrets"


@task
def create_external_models(args, ctx):
    """Create ExternalModel CRs mapping each model to the sim backend."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    sim_endpoint = f"{args.sim_service_name}.{args.namespace}.svc.cluster.local"

    for model_name in args.model_names:
        manifest = {
            "apiVersion": "maas.opendatahub.io/v1alpha1",
            "kind": "ExternalModel",
            "metadata": {
                "name": model_name,
                "namespace": args.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "forge",
                },
            },
            "spec": {
                "provider": "openai",
                "endpoint": sim_endpoint,
                "targetModel": model_name,
                "credentialRef": {
                    "name": f"{model_name}-creds",
                },
            },
        }

        oc_apply(src_dir / f"external-model-{model_name}.yaml", manifest)

    return f"Created {len(args.model_names)} ExternalModel CRs"


@task
def create_httproutes(args, ctx):
    """Create HTTPRoutes for path-based and header-based routing per model."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    for model_name in args.model_names:
        manifest = {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {
                "name": f"perf-{model_name}",
                "namespace": args.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "forge",
                },
            },
            "spec": {
                "parentRefs": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "Gateway",
                        "name": args.gateway_name,
                        "namespace": args.gateway_namespace,
                    },
                ],
                "rules": [
                    {
                        "matches": [
                            {
                                "path": {
                                    "type": "PathPrefix",
                                    "value": f"/{model_name}/",
                                },
                            },
                        ],
                        "backendRefs": [
                            {
                                "name": args.sim_service_name,
                                "port": args.sim_port,
                            },
                        ],
                        "filters": [
                            {
                                "type": "URLRewrite",
                                "urlRewrite": {
                                    "path": {
                                        "type": "ReplacePrefixMatch",
                                        "replacePrefixMatch": "/",
                                    },
                                },
                            },
                        ],
                    },
                    {
                        "matches": [
                            {
                                "headers": [
                                    {
                                        "name": "X-Gateway-Model-Name",
                                        "type": "Exact",
                                        "value": model_name,
                                    },
                                ],
                                "path": {
                                    "type": "PathPrefix",
                                    "value": "/",
                                },
                            },
                        ],
                        "backendRefs": [
                            {
                                "name": args.sim_service_name,
                                "port": args.sim_port,
                            },
                        ],
                    },
                ],
            },
        }

        oc_apply(src_dir / f"httproute-{model_name}.yaml", manifest)

    return f"Created {len(args.model_names)} HTTPRoutes"


if __name__ == "__main__":
    run.main()
