#!/usr/bin/env python3

from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, retry, task  # noqa: F401
from projects.core.dsl.utils.k8s import oc, oc_apply

logger = logging.getLogger("DSL")


@entrypoint
def run(
    *,
    namespace: str,
    name: str,
    gateway_name: str,
    gateway_namespace: str,
    backend_service: str,
    backend_port: int = 8080,
    path_prefix: str = "/",
) -> str:
    """Create an HTTPRoute pointing a Gateway to a backend service and wait for it to be accepted.

    Args:
        namespace: Namespace for the HTTPRoute resource.
        name: Name of the HTTPRoute resource.
        gateway_name: Name of the parent Gateway resource.
        gateway_namespace: Namespace of the parent Gateway resource.
        backend_service: Name of the backend Service to route traffic to.
        backend_port: Port on the backend Service.
        path_prefix: URL path prefix to match.

    Returns:
        The internal gateway endpoint URL.
    """
    ctx = execute_tasks(locals())

    return ctx.gateway_endpoint


@task
def apply_httproute(args, ctx):
    """Apply the HTTPRoute manifest."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": args.name,
            "namespace": args.namespace,
        },
        "spec": {
            "parentRefs": [
                {
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
                                "value": args.path_prefix,
                            },
                        },
                    ],
                    "backendRefs": [
                        {
                            "name": args.backend_service,
                            "port": args.backend_port,
                        },
                    ],
                },
            ],
        },
    }

    oc_apply(src_dir / "httproute.yaml", manifest)
    return f"Applied HTTPRoute {args.name}"


@retry(attempts=30, delay=10, backoff=1.0)
@task
def wait_for_accepted(args, ctx):
    """Wait until the HTTPRoute is accepted by the gateway."""
    result = oc(
        "get",
        "httproute",
        args.name,
        "-n",
        args.namespace,
        "-o",
        'jsonpath={.status.parents[*].conditions[?(@.type=="Accepted")].status}',
        log_stdout=True,
    )
    status = result.stdout.strip()
    if status != "True":
        return False, f"HTTPRoute not yet accepted (status={status!r})"

    return f"HTTPRoute {args.name} accepted by gateway"


@task
def resolve_gateway_endpoint(args, ctx):
    """Resolve the internal gateway endpoint URL."""
    gateway_class = oc(
        "get",
        "gateway",
        args.gateway_name,
        "-n",
        args.gateway_namespace,
        "-o",
        "jsonpath={.spec.gatewayClassName}",
        log_stdout=True,
    ).stdout.strip()

    ctx.gateway_endpoint = (
        f"http://{args.gateway_name}-{gateway_class}.{args.gateway_namespace}.svc.cluster.local"
    )

    return f"Gateway endpoint: {ctx.gateway_endpoint}"


if __name__ == "__main__":
    run.main()
