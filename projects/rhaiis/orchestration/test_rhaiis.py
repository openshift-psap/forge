"""RHAIIS Benchmark Implementation.

Shared logic for running vLLM benchmarks. Used by both:
- ci.py (minimal CLI for FOURNOS jobs)
- cli.py (detailed CLI for interactive use)
"""

import logging
import sys
import time
from pathlib import Path

import click

from projects.core.scenarios import ConfigLoader
from projects.core.workflow import WorkflowContext
from projects.rhaiis.workflows import BenchmarkWorkflow, CleanupWorkflow, PrepareWorkflow

logger = logging.getLogger(__name__)

# Default config directory
DEFAULT_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config" / "rhaiis"


def _dry_run_test(
    model: str | None,
    workload: str | None,
    workloads: list[str] | None,
    config_loader,
    accelerator: str,
    vllm_image: str | None,
    tensor_parallel: int | None,
    max_requests: int | None,
    namespace: str | None,
    ctx,
) -> int:
    """Show what would be executed without running."""
    global_defaults = config_loader.get_global_defaults()
    resolved_namespace = namespace or global_defaults.get("deploy", {}).get("namespace", "forge")
    resolved_max_requests = max_requests or global_defaults.get("guidellm", {}).get("max_requests", 100)
    resolved_image = vllm_image or config_loader.get_image()

    click.echo(f"[DRY-RUN] Model: {model}")
    click.echo(f"[DRY-RUN] Accelerator: {accelerator}")
    click.echo(f"[DRY-RUN] Namespace: {resolved_namespace}")
    click.echo(f"[DRY-RUN] Image: {resolved_image}")
    click.echo(f"[DRY-RUN] Max requests: {resolved_max_requests}")
    click.echo(f"[DRY-RUN] Artifacts: {ctx.artifact_dir}")

    if model:
        try:
            resolved = config_loader.load_model(model)
            click.echo(f"\n[DRY-RUN] Resolved model config ({model}):")
            click.echo(f"  HF Model ID: {resolved.hf_model_id}")
            click.echo(f"  num_gpus: {resolved.num_gpus}")
            click.echo(f"  tensor_parallel: {tensor_parallel or resolved.tensor_parallel}")
            click.echo(f"  vllm_args: {resolved.vllm_args}")
            if resolved.env_vars:
                click.echo(f"  env_vars: {resolved.env_vars}")
        except KeyError:
            click.echo(f"\n[DRY-RUN] Model '{model}' not in registry, will use defaults")

    if workloads:
        click.echo(f"\n[DRY-RUN] Workloads (deploy-once): {workloads}")
        click.echo("[DRY-RUN] vLLM will be deployed ONCE, GuideLLM runs for each workload")
    elif workload:
        click.echo(f"[DRY-RUN] Workload: {workload}")
    else:
        click.echo("[DRY-RUN] Workload: balanced (default)")

    return 0


def run_prepare(ctx: WorkflowContext, rhoai_version: str) -> int:
    """Run prepare phase - install operators."""
    workflow = PrepareWorkflow(ctx, rhoai_version=rhoai_version)
    result = workflow.execute()

    if result.success:
        click.echo(f"Prepare completed successfully in {result.duration_seconds:.1f}s")
        return 0
    else:
        click.echo(f"Prepare failed at step: {result.failed_step}", err=True)
        return 1


def run_test(
    ctx: WorkflowContext,
    model: str | None = None,
    workload: str | None = None,
    workloads: list[str] | None = None,
    config_dir: Path | None = None,
    accelerator: str = "nvidia",
    vllm_image: str | None = None,
    tensor_parallel: int | None = None,
    max_requests: int | None = None,
    namespace: str | None = None,
    dry_run: bool = False,
) -> int:
    """Run benchmark test phase.

    Args:
        ctx: Workflow context
        model: Model key or HuggingFace ID
        workload: Single workload name
        workloads: List of workloads (deploy-once pattern)
        config_dir: Config directory path
        accelerator: Accelerator type (nvidia, amd)
        vllm_image: Container image override
        tensor_parallel: TP override
        max_requests: Max GuideLLM requests
        namespace: K8s namespace
        dry_run: Print what would be done without executing

    Returns:
        Exit code (0 = success)
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR

    # Initialize ConfigLoader for inheritance-based config resolution
    config_loader = ConfigLoader(config_dir, accelerator=accelerator)

    # Dry-run: show resolved config and exit
    if dry_run:
        return _dry_run_test(
            model=model,
            workload=workload,
            workloads=workloads,
            config_loader=config_loader,
            accelerator=accelerator,
            vllm_image=vllm_image,
            tensor_parallel=tensor_parallel,
            max_requests=max_requests,
            namespace=namespace,
            ctx=ctx,
        )

    # Get defaults from config
    global_defaults = config_loader.get_global_defaults()
    default_namespace = global_defaults.get("deploy", {}).get("namespace", "forge")
    default_max_requests = global_defaults.get("guidellm", {}).get("max_requests", 100)

    # Apply defaults
    namespace = namespace or default_namespace
    max_requests = max_requests or default_max_requests

    # Get accelerator-specific image if not overridden
    if not vllm_image:
        vllm_image = config_loader.get_image()

    args = {
        "command": "test",
        "model": model,
        "workload": workload,
        "workloads": workloads,
        "accelerator": accelerator,
        "vllm_image": vllm_image,
        "tensor_parallel": tensor_parallel,
        "max_requests": max_requests,
        "namespace": namespace,
    }
    ctx.write_metadata(args)

    # Mode 1: Multiple workloads (deploy-once pattern)
    if workloads and model:
        click.echo(f"Deploy-once mode: {model} with workloads {workloads}")
        click.echo(f"Accelerator: {accelerator}")
        return _run_multi_workload(
            ctx, model, workloads, vllm_image,
            tensor_parallel, max_requests, namespace, config_loader
        )

    # Mode 2: Single workload
    elif model:
        single_workload = workload or "balanced"

        # Resolve model config for vllm_args and env_vars
        resolved_tp = tensor_parallel
        resolved_vllm_args = {}
        resolved_env_vars = {}
        resolved_model_id = model

        try:
            resolved_model = config_loader.load_model(model)
            resolved_model_id = resolved_model.hf_model_id
            resolved_vllm_args = dict(resolved_model.vllm_args)
            resolved_env_vars = dict(resolved_model.env_vars)
            if resolved_tp is None:
                resolved_tp = resolved_model.tensor_parallel
            click.echo(f"Using resolved model config: {resolved_model.key}")
            click.echo(f"  HF Model ID: {resolved_model_id}")
            click.echo(f"  vLLM args: {resolved_vllm_args}")
            if resolved_env_vars:
                click.echo(f"  Env vars: {resolved_env_vars}")
        except KeyError:
            if resolved_tp is None:
                resolved_tp = 1
            click.echo(f"Model not in registry, using defaults for: {model}")

        workflow = BenchmarkWorkflow(
            ctx,
            model=resolved_model_id,
            workload=single_workload,
            vllm_image=vllm_image,
            runtime_args=resolved_vllm_args,
            tensor_parallel=resolved_tp,
            max_requests=max_requests,
            namespace=namespace,
            env_vars=resolved_env_vars,
        )
        result = workflow.execute()

        if result.success:
            click.echo(f"\nBenchmark completed successfully in {result.duration_seconds:.1f}s")
            click.echo(f"Artifacts: {result.run_uuid}")
            return 0
        else:
            click.echo(f"\nBenchmark failed at step: {result.failed_step}", err=True)
            return 1

    else:
        click.echo("Error: Must specify --model", err=True)
        return 1


def run_cleanup(ctx: WorkflowContext, namespace: str) -> int:
    """Run cleanup phase."""
    workflow = CleanupWorkflow(ctx, namespace=namespace)
    result = workflow.execute()

    if result.success:
        click.echo(f"Cleanup completed in {result.duration_seconds:.1f}s")
        return 0
    else:
        click.echo("Cleanup had errors (check logs)", err=True)
        return 1


def _run_multi_workload(
    ctx: WorkflowContext,
    model: str,
    workload_list: list[str],
    vllm_image: str | None,
    tensor_parallel: int | None,
    max_requests: int,
    namespace: str,
    config_loader: ConfigLoader,
) -> int:
    """Run multiple workloads with deploy-once optimization.

    Groups workloads by their vllm_args - workloads with different vllm_args
    get separate deployment groups (requires vLLM restart).
    """
    from projects.core.steps import CollectArtifactsStep, CleanupDeploymentStep, RunGuideLLMStep
    from projects.rhaiis.workflows.steps import DeployVLLMStep, WaitForReadyStep

    # Load model config
    try:
        resolved = config_loader.load_model(model)
        hf_model_id = resolved.hf_model_id
        base_vllm_args = dict(resolved.vllm_args)
        env_vars = dict(resolved.env_vars)
        model_key = resolved.key
        model_tp = resolved.tensor_parallel
        click.echo(f"Using resolved model config: {model_key}")
        click.echo(f"  HF Model ID: {hf_model_id}")
        click.echo(f"  vLLM args: {base_vllm_args}")
        if env_vars:
            click.echo(f"  Env vars: {env_vars}")
    except KeyError:
        hf_model_id = model
        base_vllm_args = {}
        env_vars = {}
        model_tp = 1
        click.echo(f"Model not in registry, using defaults for: {model}")

    tensor_parallel = tensor_parallel if tensor_parallel is not None else model_tp
    deployment_name = hf_model_id.split("/")[-1].lower().replace(".", "-").replace("_", "-")[:42]

    # Group workloads by their vllm_args
    # Workloads with same vllm_args share a deployment, different vllm_args get separate deployments
    workload_groups: dict[tuple, list[str]] = {}
    for wl_key in workload_list:
        try:
            wl_config = config_loader.load_workload(wl_key)
            vllm_args_key = tuple(sorted(wl_config.vllm_args.items())) if wl_config.vllm_args else ()
        except KeyError:
            vllm_args_key = ()
        if vllm_args_key not in workload_groups:
            workload_groups[vllm_args_key] = []
        workload_groups[vllm_args_key].append(wl_key)

    num_groups = len(workload_groups)
    if num_groups > 1:
        click.echo(f"\nWorkloads grouped into {num_groups} deployment groups (different vllm_args)")

    failed = False
    total_workloads = len(workload_list)
    workload_idx = 0

    for group_idx, (vllm_args_key, group_workloads) in enumerate(workload_groups.items(), 1):
        vllm_args_override = dict(vllm_args_key) if vllm_args_key else {}

        # Merge model vllm_args with workload override
        merged_vllm_args = dict(base_vllm_args)
        merged_vllm_args.update(vllm_args_override)

        if num_groups > 1:
            click.echo(f"\n=== Deployment Group {group_idx}/{num_groups} ===")
            click.echo(f"Workloads: {group_workloads}")
            if vllm_args_override:
                click.echo(f"vllm_args override: {vllm_args_override}")

        click.echo(f"Deploying vLLM for {hf_model_id}...")

        # Deploy
        ctx.step_number += 1
        ctx.current_step_name = "deploy"
        deploy_step = DeployVLLMStep(
            model=hf_model_id,
            deployment_name=deployment_name,
            vllm_image=vllm_image,
            tensor_parallel=tensor_parallel,
            namespace=namespace,
            runtime_args=merged_vllm_args,
            env_vars=env_vars,
        )
        deploy_result = deploy_step.execute(ctx)
        if not deploy_result.success:
            click.echo(f"Deployment failed: {deploy_result.message}", err=True)
            return 1

        # Wait
        ctx.step_number += 1
        ctx.current_step_name = "wait"
        wait_step = WaitForReadyStep(
            deployment_name=deployment_name,
            namespace=namespace,
            timeout_seconds=3600,
        )
        wait_result = wait_step.execute(ctx)
        if not wait_result.success:
            click.echo(f"Wait failed: {wait_result.message}", err=True)
            CleanupDeploymentStep(deployment_name, namespace).execute(ctx)
            return 1

        click.echo("vLLM deployed successfully!")

        # Run workloads in this group
        endpoint = f"http://{deployment_name}-predictor.{namespace}.svc.cluster.local:8080/v1"

        for idx, wl in enumerate(group_workloads, 1):
            workload_idx += 1
            click.echo(f"\n--- Workload {workload_idx}/{total_workloads}: {wl} ---")

            # Load workload config to get max_seconds and rates
            try:
                wl_config = config_loader.load_workload(wl)
                wl_max_seconds = wl_config.guidellm.get("max_seconds", 300)
                wl_rates = wl_config.guidellm.get("rates", [1, 50, 100])
                wl_rate_str = ",".join(str(r) for r in wl_rates)
            except KeyError:
                wl_max_seconds = 300
                wl_rate_str = "1,50,100"

            ctx.step_number += 1
            ctx.current_step_name = f"benchmark_{wl}"
            guidellm_step = RunGuideLLMStep(
                endpoint=endpoint,
                model=hf_model_id,
                namespace=namespace,
                workload=wl,
                max_requests=max_requests,
                max_seconds=wl_max_seconds,
                rate=wl_rate_str,
            )
            result = guidellm_step.execute(ctx)

            if not result.success:
                click.echo(f"Workload {wl} failed: {result.message}", err=True)
                failed = True
                break
            else:
                click.echo(f"Workload {wl} completed successfully")
                if idx < len(group_workloads):
                    click.echo("Waiting 5s for in-flight requests to drain...")
                    time.sleep(5)

        # Cleanup this deployment before starting next group
        ctx.step_number += 1
        ctx.current_step_name = "collect_artifacts"
        click.echo("\nCollecting artifacts...")
        CollectArtifactsStep(app_label=deployment_name, namespace=namespace).execute(ctx)

        ctx.step_number += 1
        ctx.current_step_name = "cleanup"
        click.echo("Cleaning up deployment...")
        CleanupDeploymentStep(deployment_name, namespace).execute(ctx)

        if failed:
            break

    if failed:
        return 1
    else:
        click.echo(f"\nAll {total_workloads} workloads completed successfully!")
        return 0
