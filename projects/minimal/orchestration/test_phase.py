from __future__ import annotations

import copy
import logging
import signal

from projects.core.dsl.utils import slugify_identifier
from projects.core.dsl.utils.k8s import oc
from projects.core.library import config, env
from projects.core.library.postprocess import (
    create_test_metadata,
    run_and_postprocess,
    update_test_labels_with_status,
    update_test_labels_with_timing,
)
from projects.guidellm.library import benchconf as benchconf_lib
from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark import main as run_guidellm_benchmark_command
from projects.guidellm.toolbox.run_smoke_request import main as run_smoke_request_command
from projects.kserve.toolbox.deploy_k8s_vllm_sim import main as deploy_sim_command
from projects.minimal.orchestration.prepare_phase import (
    get_model_name,
    get_namespace,
    prepare,
)

logger = logging.getLogger(__name__)


def _signal_handler_sigint(sig, frame):
    env.reset_artifact_dir()


def _signal_handler_sigterm(sig, frame):
    env.reset_artifact_dir()


def _setup_signal_handlers():
    try:
        signal.signal(signal.SIGINT, _signal_handler_sigint)
        signal.signal(signal.SIGTERM, _signal_handler_sigterm)
    except Exception as e:
        logger.warning(f"Failed to set up signal handlers: {e}")


def get_benchmark_key() -> str | None:
    return config.project.get_config("runtime.benchmark_key", default_value=None)


def get_benchmark_config() -> dict | None:
    benchmark_key = get_benchmark_key()
    if not benchmark_key:
        return None

    benchmark = copy.deepcopy(
        config.project.get_config(f"workloads.benchmarks['{benchmark_key}']", print=False)
    )
    workload_defaults = copy.deepcopy(config.project.get_config("workloads", print=False))

    default_keys = ("job_name", "image", "pvc_size", "pvc_storage_class", "timeout_seconds")
    for key in default_keys:
        if key in workload_defaults and key not in benchmark:
            benchmark[key] = workload_defaults[key]

    benchmark_args = benchmark.get("args", {})
    workload_args = workload_defaults.get("args", {})
    if workload_args:
        merged = copy.deepcopy(workload_args)
        merged.update(benchmark_args)
        benchmark["args"] = merged

    return benchmark


def get_smoke_request() -> dict:
    smoke_request_key = "default"
    return config.project.get_config(f"workloads.smoke_requests['{smoke_request_key}']")


def get_sim_config() -> dict:
    return config.project.get_config("runtime.sim")


def test():
    """Main test function that wraps do_test() with outcome postprocessing."""
    return run_and_postprocess(do_test)


def create_custom_test_metadata():
    model_name = get_model_name()
    benchmark_key = get_benchmark_key()

    labels = {
        "model_name": model_name,
    }
    if benchmark_key:
        labels["benchmark_key"] = benchmark_key

    test_dir = env.ARTIFACT_DIR
    create_test_metadata(test_dir, labels)
    return test_dir


def do_test():
    logger.info("=== Minimal Project Test Phase ===")

    namespace = get_namespace()
    sim_config = get_sim_config()
    sim_name = sim_config["name"]
    model_name = get_model_name()

    endpoint_url = None
    test_dir = None

    with env.NextArtifactDir("minimal_test"):
        test_dir = create_custom_test_metadata()
        try:
            update_test_labels_with_timing(test_dir, "test", "start")

            prepare()

            with env.NextArtifactDir("deploy_sim"):
                endpoint_url = deploy_sim_command.run(
                    namespace=namespace,
                    name=sim_name,
                    model_name=model_name,
                    image=sim_config["image"],
                    max_num_seqs=sim_config["max_num_seqs"],
                    max_model_len=sim_config["max_model_len"],
                    time_to_first_token=sim_config["time_to_first_token"],
                    inter_token_latency=sim_config["inter_token_latency"],
                    mode=sim_config["mode"],
                )

            run_smoke_request_test(endpoint_url=endpoint_url)

            run_guidellm_benchmark(test_dir, endpoint_url=endpoint_url)

        except Exception as e:
            logger.exception("Test failed with exception")
            update_test_labels_with_status(test_dir, False, f"Test failed: {str(e)}")
            raise
        finally:
            update_test_labels_with_timing(test_dir, "test", "end")

            cleanup_sim(namespace=namespace, name=sim_name)

    update_test_labels_with_status(test_dir, True, "Test completed successfully")
    return 0


def cleanup_sim(*, namespace: str, name: str) -> None:
    with env.NextArtifactDir("cleanup_sim"):
        oc(
            "delete",
            "deployment",
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            check=False,
        )
        oc(
            "delete",
            "service",
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            check=False,
        )


def run_smoke_request_test(*, endpoint_url: str) -> None:
    namespace = get_namespace()
    smoke_request = get_smoke_request()
    model_name = get_model_name()

    with env.NextArtifactDir("smoke_request"):
        run_smoke_request_command.run(
            namespace=namespace,
            endpoint_url=endpoint_url,
            served_model_name=model_name,
            prompt=smoke_request["prompt"],
            max_tokens=smoke_request["max_tokens"],
            temperature=smoke_request["temperature"],
        )


def run_guidellm_benchmark(test_dir, *, endpoint_url: str) -> None:
    namespace = get_namespace()
    benchmark = get_benchmark_config()

    if benchmark is None:
        logger.info("No benchmark configured, skipping")
        return

    update_test_labels_with_timing(test_dir, "benchmark", "start")

    try:
        benchmark_key = get_benchmark_key()

        config_path = None
        benchconf_ref = benchmark.get("benchconf")
        if benchconf_ref and benchconf_lib._is_enabled():
            benchconf_lib.maybe_install_custom_version()
            config_path = benchconf_lib.resolve_config_path(benchconf_ref)
            benchconf_lib.save_version()

        guidellm_args = build_guidellm_args(benchmark)
        if not any(arg.startswith(("--tokenizer=", "--processor=")) for arg in guidellm_args):
            guidellm_args.append(f"--tokenizer=kind=huggingface_auto,model={get_model_name()}")

        artifact_name = f"benchmark_{slugify_identifier(benchmark_key, max_length=48)}"
        with env.NextArtifactDir(artifact_name):
            run_guidellm_benchmark_command.run(
                endpoint_url=endpoint_url,
                name=benchmark.get("job_name"),
                namespace=namespace,
                image=benchmark.get("image"),
                timeout=benchmark.get("timeout_seconds"),
                pvc_size=benchmark.get("pvc_size"),
                pvc_storage_class=benchmark.get("pvc_storage_class"),
                guidellm_args=guidellm_args,
                config_path=config_path,
                use_pvc=benchmark.get("use_pvc"),
            )
    finally:
        update_test_labels_with_timing(test_dir, "benchmark", "end")


def fournos_resolve_hardware_request(hardware_spec: dict):
    """Resolve hardware requirements for FournosJob."""

    return hardware_spec
