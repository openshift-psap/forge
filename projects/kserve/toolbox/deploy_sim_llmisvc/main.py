#!/usr/bin/env python3

from __future__ import annotations

import logging

import yaml

from projects.core.dsl import (  # noqa: F401
    entrypoint,
    execute_tasks,
    retry,
    task,
)
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import (
    oc_apply,
)
from projects.kserve.toolbox.deploy_llmisvc import main as deploy_llmisvc

logger = logging.getLogger("DSL")

SIM_IMAGE = "ghcr.io/llm-d/llm-d-inference-sim:latest"


@entrypoint
def run(
    *,
    namespace: str,
    name: str = "sim-llm",
    model_name: str = "simulated-llama-3",
    image: str = SIM_IMAGE,
    gateway_name: str = "",
    gateway_namespace: str = "",
    gateway_status_address_name: str | None = "gateway-external",
    max_num_seqs: int = 2048,
    max_model_len: int = 8192,
    time_to_first_token: str = "800ms",
    inter_token_latency: str = "30ms",
    mode: str = "random",
    deploy_monitor: bool = True,
    skip_router: bool = False,
) -> str:
    """Build and deploy an llm-d simulator LLMInferenceService.

    Args:
        namespace: Namespace for the LLMInferenceService.
        name: Name of the LLMInferenceService resource.
        model_name: Model name advertised by the simulator.
        image: Container image for the simulator.
        gateway_name: Gateway resource name. Leave empty to omit gateway config.
        gateway_namespace: Namespace of the gateway resource.
        gateway_status_address_name: Gateway status address name for endpoint resolution.
        max_num_seqs: Maximum number of concurrent sequences.
        max_model_len: Maximum model context length.
        time_to_first_token: Simulated time to first token.
        inter_token_latency: Simulated inter-token latency.
        mode: Simulator response mode.
        deploy_monitor: If True, deploy ServiceMonitor for monitoring.
        skip_router: If True, set spec.router to empty (no scheduler, no gateway routing).
    """
    ctx = execute_tasks(locals())

    return ctx.endpoint_url


@task
def build_manifest(args, ctx):
    """Render the llm-d simulator LLMInferenceService manifest."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    manifest = yaml.safe_load(
        render_template(
            "llmisvc.yaml.j2",
            context={
                "name": args.name,
                "namespace": args.namespace,
                "model_name": args.model_name,
                "image": args.image,
                "gateway_name": args.gateway_name,
                "gateway_namespace": args.gateway_namespace,
                "max_num_seqs": args.max_num_seqs,
                "max_model_len": args.max_model_len,
                "time_to_first_token": args.time_to_first_token,
                "inter_token_latency": args.inter_token_latency,
                "mode": args.mode,
                "skip_router": args.skip_router,
            },
        )
    )

    manifest_path = src_dir / "sim-llmisvc.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    ctx.manifest_path = str(manifest_path)
    return f"Built sim LLMInferenceService manifest at {manifest_path}"


@task
def deploy(args, ctx):
    """Deploy the simulator LLMInferenceService via deploy_llmisvc."""
    endpoint_url = deploy_llmisvc.run(
        namespace=args.namespace,
        inference_service_manifest_path=ctx.manifest_path,
        gateway_status_address_name=args.gateway_status_address_name,
        deploy_monitor=args.deploy_monitor,
    )

    ctx.endpoint_url = endpoint_url
    return f"Deployed sim LLMInferenceService, endpoint: {endpoint_url}"


@retry(attempts=18, delay=10, backoff=1.0)
@task
def apply_destination_rule(args, ctx):
    """Create a DestinationRule to disable TLS to the InferencePool backend service."""
    if args.skip_router:
        return "Skipped DestinationRule (no router configured)"

    from projects.core.dsl.utils.k8s import oc

    pool_svc_result = oc(
        "get",
        "svc",
        "-n",
        args.namespace,
        "-l",
        "internal.istio.io/service-semantics=inferencepool",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    )
    pool_svc_name = pool_svc_result.stdout.strip()
    if not pool_svc_name:
        logger.info("InferencePool service not found yet, retrying...")
        return False

    llmisvc_uid = oc(
        "get",
        "llminferenceservice",
        args.name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.metadata.uid}",
    ).stdout.strip()

    pool_host = f"{pool_svc_name}.{args.namespace}.svc.cluster.local"

    dr_manifest = {
        "apiVersion": "networking.istio.io/v1",
        "kind": "DestinationRule",
        "metadata": {
            "name": f"{args.name}-plaintext",
            "namespace": args.namespace,
            "ownerReferences": [
                {
                    "apiVersion": "serving.kserve.io/v1alpha1",
                    "kind": "LLMInferenceService",
                    "name": args.name,
                    "uid": llmisvc_uid,
                },
            ],
        },
        "spec": {
            "host": pool_host,
            "trafficPolicy": {
                "tls": {
                    "mode": "DISABLE",
                },
            },
        },
    }

    dest_rule_path = args.artifact_dir / "src" / "destination-rule.yaml"
    oc_apply(dest_rule_path, dr_manifest)

    return f"Applied DestinationRule {args.name}-plaintext for host {pool_host}"


if __name__ == "__main__":
    run.main()
