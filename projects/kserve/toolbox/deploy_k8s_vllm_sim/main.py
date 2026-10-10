from __future__ import annotations

import logging

import yaml

from projects.core.dsl import (
    always,
    entrypoint,
    execute_tasks,
    retry,
    task,
)
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import oc, oc_apply

logger = logging.getLogger("TOOLBOX")

SIM_IMAGE = "ghcr.io/llm-d/llm-d-inference-sim:latest"


@entrypoint
def run(
    *,
    namespace: str,
    name: str = "sim-llm",
    model_name: str = "simulated-llama-3",
    image: str = SIM_IMAGE,
    port: int = 8000,
    replicas: int = 1,
    max_num_seqs: int = 2048,
    max_model_len: int = 8192,
    time_to_first_token: str = "800ms",
    inter_token_latency: str = "30ms",
    mode: str = "random",
) -> str:
    """Deploy an llm-d simulator as a Deployment + Service.

    Args:
        namespace: Namespace for the deployment.
        name: Name of the Deployment and Service.
        model_name: Model name advertised by the simulator.
        image: Container image for the simulator.
        port: Port the simulator listens on.
        replicas: Number of replicas.
        max_num_seqs: Maximum number of concurrent sequences.
        max_model_len: Maximum model context length.
        time_to_first_token: Simulated time to first token.
        inter_token_latency: Simulated inter-token latency.
        mode: Simulator response mode.
    """
    ctx = execute_tasks(locals())

    return ctx.endpoint_url


@task
def create_deployment(args, ctx):
    """Render and apply the sim Deployment."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    manifest = yaml.safe_load(
        render_template(
            "deployment.yaml.j2",
            context={
                "name": args.name,
                "namespace": args.namespace,
                "model_name": args.model_name,
                "image": args.image,
                "port": args.port,
                "replicas": args.replicas,
                "max_num_seqs": args.max_num_seqs,
                "max_model_len": args.max_model_len,
                "time_to_first_token": args.time_to_first_token,
                "inter_token_latency": args.inter_token_latency,
                "mode": args.mode,
            },
        )
    )

    oc_apply(src_dir / "deployment.yaml", manifest)
    return f"Deployment {args.name} applied"


@task
def create_service(args, ctx):
    """Render and apply the sim Service."""
    src_dir = args.artifact_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    manifest = yaml.safe_load(
        render_template(
            "service.yaml.j2",
            context={
                "name": args.name,
                "namespace": args.namespace,
                "port": args.port,
            },
        )
    )

    oc_apply(src_dir / "service.yaml", manifest)
    ctx.endpoint_url = f"http://{args.name}.{args.namespace}.svc:{args.port}"
    return f"Service {args.name} applied, endpoint: {ctx.endpoint_url}"


@retry(attempts=120, delay=15, backoff=1.0)
@task
def wait_deployment_ready(args, ctx):
    """Wait for the sim Deployment to have available replicas."""
    result = oc(
        "get",
        "deployment",
        args.name,
        "-n",
        args.namespace,
        "-o",
        "jsonpath={.status.availableReplicas}",
        log_stdout=True,
    )

    available = result.stdout.strip()
    if not available or int(available) < args.replicas:
        return False

    return f"Deployment {args.name} ready with {available} replicas"


@always
@task
def capture_state(args, ctx):
    """Capture deployment and pod state for debugging."""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "get",
        "deployment",
        args.name,
        "-n",
        args.namespace,
        "-o",
        "yaml",
        check=False,
        stdout_dest=artifacts_dir / "deployment.yaml",
    )

    oc(
        "get",
        "pods",
        "-l",
        f"app={args.name}",
        "-n",
        args.namespace,
        "-o",
        "wide",
        check=False,
        stdout_dest=artifacts_dir / "pods.txt",
    )

    oc(
        "describe",
        "pods",
        "-l",
        f"app={args.name}",
        "-n",
        args.namespace,
        check=False,
        stdout_dest=artifacts_dir / "pod_descriptions.txt",
    )

    oc(
        "logs",
        "-l",
        f"app={args.name}",
        "-n",
        args.namespace,
        "--tail=200",
        "--all-containers",
        check=False,
        stdout_dest=artifacts_dir / "pod_logs.txt",
    )

    return "Captured sim deployment state"


if __name__ == "__main__":
    run.main()
