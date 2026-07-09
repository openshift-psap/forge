from __future__ import annotations

import logging

from projects.core.library import env
from projects.core.library.postprocess import run_and_postprocess, write_test_labels
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


def run(
    *,
    model_key: str,
    workload_key: str,
    namespace: str,
    deployment_name: str | None = None,
) -> int:
    return run_and_postprocess(
        do_test,
        model_key=model_key,
        workload_key=workload_key,
        namespace=namespace,
        deployment_name=deployment_name,
    )


def do_test(
    *,
    model_key: str,
    workload_key: str,
    namespace: str,
    deployment_name: str | None = None,
) -> int:
    with env.NextArtifactDir("testing"):
        return _run_test(
            model_key=model_key,
            workload_key=workload_key,
            namespace=namespace,
            deployment_name=deployment_name,
        )


def _run_test(
    *,
    model_key: str,
    workload_key: str,
    namespace: str,
    deployment_name: str | None = None,
) -> int:
    model_cfg = runtime_config.get_model(model_key)
    workload = runtime_config.get_workload(workload_key)
    accelerator = runtime_config.get_accelerator()
    deploy_cfg = runtime_config.get_deploy_config()
    benchmark_cfg = runtime_config.get_benchmark_config()

    if not deployment_name:
        deployment_name = runtime_config.derive_deployment_name(model_cfg["hf_model_id"])

    vllm_image = runtime_config.get_vllm_image(accelerator)
    vllm_defaults = runtime_config.get_vllm_defaults()
    vllm_args = runtime_config.merge_vllm_args(vllm_defaults, model_cfg, workload)
    env_vars = runtime_config.merge_env_vars(accelerator, model_cfg)

    rates = workload.get("rates", [1])
    max_seconds = workload.get("max_seconds", 180)

    _create_test_labels(model_key, workload_key, accelerator, vllm_args)

    logger.info(
        "Testing model=%s workload=%s accelerator=%s", model_cfg["name"], workload_key, accelerator
    )

    from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
        run as run_guidellm_benchmark,
    )
    from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
        wait_guidellm_benchmark_task,
    )
    from projects.rhaiis.toolbox.deploy_kserve_isvc.main import run as deploy_kserve_isvc
    from projects.rhaiis.toolbox.wait_isvc_ready.main import run as wait_isvc_ready

    benchmark_timeout = benchmark_cfg.get("timeout", 14400)
    wait_guidellm_benchmark_task._retry_config["attempts"] = max(1, benchmark_timeout // 10)

    try:
        logger.info("Deploying %s to %s/%s", model_cfg["hf_model_id"], namespace, deployment_name)
        deploy_kserve_isvc(
            deployment_name=deployment_name,
            namespace=namespace,
            model_id=model_cfg["hf_model_id"],
            vllm_image=vllm_image,
            accelerator=accelerator,
            vllm_args=vllm_args,
            env_vars=env_vars,
            replicas=deploy_cfg.get("replicas", 1),
            cpu_request=deploy_cfg.get("cpu_request", "4"),
            memory_request=deploy_cfg.get("memory_request", "16Gi"),
            storage_source=deploy_cfg.get("storage_source", "hf"),
            storage_pvc=deploy_cfg.get("storage_pvc", ""),
            image_pull_secret=deploy_cfg.get("image_pull_secret", ""),
            service_account_name=deploy_cfg.get("service_account_name", ""),
        )

        logger.info("Waiting for InferenceService to be ready")
        wait_isvc_ready(
            name=deployment_name,
            namespace=namespace,
            timeout_seconds=deploy_cfg.get("ready_timeout", 3600),
            health_check_timeout=deploy_cfg.get("health_check_timeout", 120),
        )

        endpoint_url = f"http://{deployment_name}-predictor.{namespace}.svc.cluster.local:8080"

        logger.info("Running benchmark at rates=%s", rates)

        benchmark_image = benchmark_cfg.get("image", "ghcr.io/vllm-project/guidellm:v0.6.0")

        guidellm_args = runtime_config.build_guidellm_args(
            benchmark_cfg=benchmark_cfg,
            model_id=model_cfg["hf_model_id"],
            data=workload["data"],
            rates=rates,
            max_seconds=max_seconds,
        )

        run_guidellm_benchmark(
            endpoint_url=f"{endpoint_url}/v1",
            name=f"guidellm-{deployment_name}",
            namespace=namespace,
            image=benchmark_image,
            timeout=benchmark_timeout,
            pvc_size=benchmark_cfg.get("pvc_size", "5Gi"),
            guidellm_args=guidellm_args,
        )
    finally:
        _capture_and_cleanup(deployment_name, namespace)

    try:
        _generate_psap_payload(model_cfg, accelerator, vllm_image, vllm_args, workload_key)
    except Exception:
        logger.warning("PSAP payload generation failed; continuing", exc_info=True)

    try:
        _set_mlflow_metadata(
            model_key,
            workload_key,
            model_cfg,
            accelerator,
            vllm_image,
            vllm_args,
            benchmark_cfg,
            rates,
            max_seconds,
            namespace,
            deployment_name,
        )
    except Exception:
        logger.warning("Setting MLflow metadata failed; continuing", exc_info=True)

    return 0


def _create_test_labels(
    model_key: str, workload_key: str, accelerator: str, vllm_args: dict
) -> None:
    labels = {
        "model_key": model_key,
        "workload_key": workload_key,
        "accelerator": accelerator,
        "tensor_parallel_size": str(vllm_args.get("tensor-parallel-size", 1)),
    }
    write_test_labels(env.ARTIFACT_DIR, labels)
    logger.info("Created test labels: %s", labels)


def _set_mlflow_metadata(
    model_key: str,
    workload_key: str,
    model_cfg: dict,
    accelerator: str,
    vllm_image: str,
    vllm_args: dict,
    benchmark_cfg: dict,
    rates: list[int],
    max_seconds: int,
    namespace: str,
    deployment_name: str,
) -> None:
    from projects.core.library import config

    image_name, image_tag = runtime_config.split_image_tag(vllm_image)
    guidellm_image = benchmark_cfg.get("image", "ghcr.io/vllm-project/guidellm:v0.6.0")
    benchmark_args = benchmark_cfg.get("args", {})

    tags = {
        "project": "rhaiis",
        "model_key": model_key,
        "hf_model_id": model_cfg["hf_model_id"],
        "accelerator": accelerator,
        "tensor_parallel_size": str(vllm_args.get("tensor-parallel-size", 1)),
        "vllm_image": vllm_image,
        "vllm_version": image_tag,
        "workload_key": workload_key,
        "rates": ",".join(str(r) for r in rates),
        "max_seconds": str(max_seconds),
        "guidellm_image": guidellm_image,
        "namespace": namespace,
        "deployment_name": deployment_name,
    }
    for key, value in benchmark_args.items():
        tags[f"guidellm_{key}"] = str(value)

    config.project.set_config("caliper.export.backend.mlflow.config.tags", tags)
    logger.info("Set MLflow tags: %s", list(tags.keys()))


def _generate_psap_payload(
    model_cfg: dict,
    accelerator: str,
    vllm_image: str,
    vllm_args: dict,
    workload_key: str,
) -> None:
    from pathlib import Path

    from projects.rhaiis.postprocess.parser import generate_psap_payload, write_psap_payload

    matches = list(
        Path(env.ARTIFACT_DIR).glob("*__run_guidellm_benchmark/artifacts/results/benchmarks.json")
    )
    if not matches:
        logger.warning(
            "benchmarks.json not found under %s, skipping PSAP payload", env.ARTIFACT_DIR
        )
        return
    benchmarks_json = matches[0]

    payload = generate_psap_payload(
        benchmarks_json_path=benchmarks_json,
        model_id=model_cfg["hf_model_id"],
        vllm_image=vllm_image,
        vllm_args=vllm_args,
        accelerator=accelerator,
        workload_key=workload_key,
    )
    output_dir = Path(env.ARTIFACT_DIR) / "artifacts" / "results"
    write_psap_payload(
        payload=payload,
        output_dir=output_dir,
        accelerator=accelerator,
        model_id=model_cfg["hf_model_id"],
        workload_key=workload_key,
    )


def _capture_and_cleanup(deployment_name: str, namespace: str) -> None:
    from projects.rhaiis.toolbox.capture_isvc_state.main import run as capture_isvc_state

    logger.info("Capturing state")
    try:
        capture_isvc_state(name=deployment_name, namespace=namespace)
    except Exception:
        logger.warning("Capture failed, continuing with cleanup", exc_info=True)

    from projects.rhaiis.toolbox.cleanup_isvc.main import run as cleanup_isvc

    logger.info("Cleaning up")
    try:
        cleanup_isvc(name=deployment_name, namespace=namespace)
    except Exception:
        logger.warning("Cleanup failed", exc_info=True)
