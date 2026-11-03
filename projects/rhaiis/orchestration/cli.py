#!/usr/bin/env python3
"""RHAIIS CLI - Interactive CLI for KServe InferenceService benchmarking.

Examples:
    # Quick test with defaults (qwen3-0.6b, balanced workload)
    python -m projects.rhaiis.orchestration.cli test \
        --namespace kserve-e2e-perf

    # Specific model and workload
    python -m projects.rhaiis.orchestration.cli test \
        --model llama-3-1-8b-fp8 \
        --workload short \
        --namespace kserve-e2e-perf \
        --image-pull-secret npalaska-image-pull

    # Dry run
    python -m projects.rhaiis.orchestration.cli test \
        --model qwen3-0_6b --dry-run

    # Cleanup only
    python -m projects.rhaiis.orchestration.cli cleanup \
        --deployment-name rhaiis-bench \
        --namespace kserve-e2e-perf
"""

import logging
import types

import click

from projects.core.library import config
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


@click.group()
@click.pass_context
def cli(ctx):
    """RHAIIS CLI - KServe InferenceService benchmarking."""
    ctx.ensure_object(types.SimpleNamespace)
    runtime_config.init()


@cli.command()
@click.option(
    "--preset", "-p", multiple=True, help="Preset name(s) from presets.d/ (e.g. llama-8b profile1)"
)
@click.option("--model", "-m", default=None, help="Model key from config.yaml")
@click.option("--workload", "-w", default=None, help="Workload profile name")
@click.option("--namespace", "-n", default=None, help="Kubernetes namespace")
@click.option("--deployment-name", default=None, help="Deployment name (defaults to model name)")
@click.option("--accelerator", type=click.Choice(["nvidia", "amd"]), default=None)
@click.option(
    "--engine",
    type=click.Choice(["vllm", "sglang", "trtllm"]),
    default=None,
    help="Inference engine",
)
@click.option("--serving-image", help="Serving engine container image override")
@click.option("--tensor-parallel", "-tp", type=int, help="Tensor parallel size override")
@click.option("--replicas", "-r", type=int, default=None)
@click.option("--storage-source", type=click.Choice(["hf", "pvc"]), default=None)
@click.option("--storage-pvc", help="PVC name for model storage")
@click.option("--image-pull-secret", help="Image pull secret name")
@click.option("--service-account-name", help="Service account name for predictor")
@click.option("--max-seconds", type=int, help="Max benchmark duration per rate")
@click.option("--rates", help="Comma-separated rates (e.g. 1,50,100)")
@click.option("--dry-run", is_flag=True, help="Print what would be done")
@click.pass_context
def test(
    ctx,
    preset: tuple[str, ...],
    model: str | None,
    workload: str | None,
    namespace: str | None,
    deployment_name: str | None,
    accelerator: str | None,
    engine: str | None,
    serving_image: str | None,
    tensor_parallel: int | None,
    replicas: int | None,
    storage_source: str | None,
    storage_pvc: str | None,
    image_pull_secret: str | None,
    service_account_name: str | None,
    max_seconds: int | None,
    rates: str | None,
    dry_run: bool,
):
    """Run KServe InferenceService benchmark."""
    for name in preset:
        config.project.apply_preset(name)

    model_key = model or runtime_config.get_test_model_key()
    workload_keys = [workload] if workload else runtime_config.get_test_workload_keys()
    workload_key = workload_keys[0]
    namespace = namespace or runtime_config.get_namespace()

    # Apply CLI overrides to config so test_phase reads them via runtime_config
    _apply_cli_overrides(
        workload_key=workload_key,
        accelerator=accelerator,
        engine=engine,
        serving_image=serving_image,
        tensor_parallel=tensor_parallel,
        replicas=replicas,
        storage_source=storage_source,
        storage_pvc=storage_pvc,
        image_pull_secret=image_pull_secret,
        service_account_name=service_account_name,
        rates=rates,
        max_seconds=max_seconds,
    )

    if dry_run:
        for selected_workload in workload_keys:
            _print_dry_run(model_key, selected_workload, namespace, deployment_name)
        return

    from projects.rhaiis.orchestration import test_phase

    try:
        test_phase.run(
            model_key=model_key,
            workload_keys=workload_keys,
            namespace=namespace,
            deployment_name=deployment_name,
        )
    except Exception as exc:
        click.echo(f"Run failed: {exc}")
        raise SystemExit(1) from exc

    click.echo("Benchmark completed successfully.")


def _apply_cli_overrides(
    *,
    workload_key: str,
    accelerator: str | None,
    engine: str | None,
    serving_image: str | None,
    tensor_parallel: int | None,
    replicas: int | None,
    storage_source: str | None,
    storage_pvc: str | None,
    image_pull_secret: str | None,
    service_account_name: str | None,
    rates: str | None,
    max_seconds: int | None,
) -> None:
    if accelerator:
        config.project.set_config("rhaiis.accelerator", accelerator)
    if engine:
        config.project.set_config("rhaiis.engine", engine)
    resolved_engine = runtime_config.get_engine()
    if serving_image:
        resolved_accel = runtime_config.get_accelerator()
        config.project.set_config(
            f"rhaiis.engines.{resolved_engine}.images.{resolved_accel}", serving_image
        )
    if tensor_parallel is not None:
        tp_key = {"sglang": "tp-size", "trtllm": "tp_size"}.get(
            resolved_engine, "tensor-parallel-size"
        )
        config.project.set_config(
            f"rhaiis.engines.{resolved_engine}.args.{tp_key}", tensor_parallel
        )
    if replicas is not None:
        config.project.set_config("rhaiis.deploy.replicas", replicas)
    if storage_source:
        config.project.set_config("rhaiis.deploy.storage_source", storage_source)
    if storage_pvc:
        config.project.set_config("rhaiis.deploy.storage_pvc", storage_pvc)
    if image_pull_secret:
        config.project.set_config("rhaiis.deploy.image_pull_secret", image_pull_secret)
    if service_account_name:
        config.project.set_config("rhaiis.deploy.service_account_name", service_account_name)
    if rates:
        rate_list = [int(r) for r in rates.split(",")]
        config.project.set_config(f"workloads.{workload_key}.rates", rate_list)
    if max_seconds is not None:
        config.project.set_config(f"workloads.{workload_key}.max_seconds", max_seconds)


def _print_dry_run(
    model_key: str, workload_key: str, namespace: str, deployment_name: str | None
) -> None:
    model_cfg = runtime_config.get_model(model_key)
    workload_cfg = runtime_config.get_workload(workload_key)
    deploy_cfg = runtime_config.get_deploy_config()
    accelerator = runtime_config.get_accelerator()
    engine = runtime_config.get_engine()
    serving_image = runtime_config.get_serving_image(accelerator, engine)
    engine_defaults = runtime_config.get_engine_args(engine)
    engine_args = runtime_config.merge_engine_args(engine_defaults, model_cfg, workload_cfg, engine)
    env_vars = runtime_config.merge_env_vars(accelerator, model_cfg)

    if not deployment_name:
        deployment_name = runtime_config.derive_deployment_name(model_cfg["hf_model_id"])

    click.echo("[DRY-RUN] RHAIIS Benchmark Test")
    click.echo(f"  Model: {model_key} ({model_cfg['hf_model_id']})")
    click.echo(f"  Workload: {workload_key}")
    click.echo(f"  Namespace: {namespace}")
    click.echo(f"  Deployment: {deployment_name}")
    click.echo(f"  Engine: {engine}")
    click.echo(f"  Accelerator: {accelerator}")
    click.echo(f"  Image: {serving_image}")
    click.echo(f"  Engine args: {engine_args}")
    click.echo(f"  Env vars: {env_vars}")
    click.echo(f"  Replicas: {deploy_cfg.get('replicas', 1)}")
    click.echo(
        f"  Storage: {deploy_cfg.get('storage_source', 'hf')} (pvc={deploy_cfg.get('storage_pvc', '')})"
    )
    click.echo(f"  Image pull secret: {deploy_cfg.get('image_pull_secret') or '(none)'}")
    click.echo(f"  Service account: {deploy_cfg.get('service_account_name') or '(none)'}")
    if benchconf_ref := workload_cfg.get("benchconf"):
        click.echo(f"  BenchConf: {benchconf_ref}")
    else:
        click.echo(f"  Rates: {workload_cfg.get('rates', [1])}")
        click.echo(f"  Max seconds: {workload_cfg.get('max_seconds', 180)}")


@cli.command()
@click.option("--deployment-name", required=True, help="InferenceService name")
@click.option("--namespace", "-n", default="forge-rhaiis", help="Kubernetes namespace")
@click.pass_context
def cleanup(ctx, deployment_name: str, namespace: str):
    """Cleanup InferenceService deployment."""
    from projects.rhaiis.toolbox.capture_isvc_state.main import run as capture_isvc_state
    from projects.rhaiis.toolbox.cleanup_isvc.main import run as cleanup_isvc

    click.echo(f"Capturing state for {deployment_name}...")
    try:
        capture_isvc_state(name=deployment_name, namespace=namespace)
    except Exception as exc:
        click.echo(f"Warning: capture failed: {exc}")

    click.echo(f"Cleaning up {deployment_name}...")
    cleanup_isvc(name=deployment_name, namespace=namespace)

    click.echo("Cleanup completed.")


if __name__ == "__main__":
    cli()
