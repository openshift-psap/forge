#!/usr/bin/env python3

from __future__ import annotations

import logging

import yaml

from projects.core.dsl import (  # noqa: F401
    entrypoint,
    execute_tasks,
    task,
)
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import oc
from projects.kserve.toolbox.deploy_llmisvc import main as deploy_llmisvc

logger = logging.getLogger("DSL")

SIM_IMAGE = "ghcr.io/llm-d/llm-d-inference-sim:latest"


@entrypoint
def run(
    *,
    namespace: str,
    name: str = "sim-llm",
    model_name: str = "simulated-model",
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
    maas_gateway_label: str = "",
    served_model_names: list[str] | None = None,
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
        maas_gateway_label: MaaS gateway label value. When set, adds MaaS labels and annotations.
        served_model_names: Additional model names to serve (--served-model-name flags).
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
                "deploy_monitor": args.deploy_monitor,
                "maas_gateway_label": args.maas_gateway_label,
                "served_model_names": args.served_model_names or [],
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


@task
def cleanup_router_servicemonitor(args, ctx):
    """Remove the router ServiceMonitor when skip_router is enabled."""
    if not args.skip_router:
        return "Router enabled, keeping epp ServiceMonitor"

    oc(
        "delete",
        "servicemonitor",
        f"{args.name}-epp",
        "-n",
        args.namespace,
        "--ignore-not-found=true",
    )
    return f"Deleted router ServiceMonitor {args.name}-epp (skip_router=True)"


if __name__ == "__main__":
    run.main()
