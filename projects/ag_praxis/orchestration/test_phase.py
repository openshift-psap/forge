import logging

from projects.ag_praxis.toolbox.deploy_praxis import main as deploy_praxis_command
from projects.core.library import config, env
from projects.core.library.postprocess import (
    create_test_metadata,
    run_and_postprocess,
    update_test_labels_with_status,
    update_test_labels_with_timing,
)

logger = logging.getLogger(__name__)

SIM_LLMISVC_NAME = "sim-llm"
SIM_MODEL_NAME = "simulated-llama-3"


def test_maas():
    from projects.ag_praxis.toolbox.maas_sim_test import main as maas_sim_test_command

    maas_sim_test_command.run(action="status")
    maas_sim_test_command.run(action="cleanup")
    maas_sim_test_command.run(action="deploy")
    maas_sim_test_command.run(action="status")
    maas_sim_test_command.run(action="test")
    maas_sim_test_command.run(action="cleanup")
    maas_sim_test_command.run(action="status")


def test():
    """Main test function that wraps do_test() with outcome postprocessing."""
    return run_and_postprocess(do_test)


def create_custom_test_metadata():
    labels = {
        "praxis": True,
    }
    test_dir = env.ARTIFACT_DIR
    create_test_metadata(
        test_dir,
        labels,
    )

    return test_dir


def _resolve_benchmark_config(benchmark_key: str) -> dict:
    """Resolve a benchmark config by key, merging workload defaults."""
    import copy

    workloads = copy.deepcopy(config.project.get_config("workloads"))
    benchmark = copy.deepcopy(workloads.get("benchmarks", {}).get(benchmark_key))
    if benchmark is None:
        raise ValueError(f"Unknown benchmark key: {benchmark_key}")

    for key in (
        "job_name",
        "image",
        "pvc_size",
        "pvc_storage_class",
        "timeout_seconds",
        "fs_group",
        "use_pvc",
    ):
        if key in workloads and key not in benchmark:
            benchmark[key] = workloads[key]

    workload_args = workloads.get("args", {})
    if workload_args:
        benchmark_args = benchmark.get("args", {})
        merged = dict(workload_args)
        merged.update(benchmark_args)
        benchmark["args"] = merged

    return benchmark


def _run_guidellm(test_namespace: str, endpoint_url: str):
    """Run the GuideLLM benchmark with the configured profile."""
    from projects.guidellm.library import benchconf as benchconf_lib
    from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
    from projects.guidellm.toolbox.run_guidellm_benchmark import (
        main as run_guidellm_benchmark_command,
    )

    benchmark_key = config.project.get_config("test.benchmark_key")
    benchmark = _resolve_benchmark_config(benchmark_key)

    config_path = None
    benchconf_ref = benchmark.get("benchconf")
    if benchconf_ref and benchconf_lib._is_enabled():
        benchconf_lib.maybe_install_custom_version()
        config_path = benchconf_lib.resolve_config_path(benchconf_ref)
        benchconf_lib.save_version()

    guidellm_args = build_guidellm_args(benchmark)

    run_guidellm_benchmark_command.run(
        endpoint_url=endpoint_url,
        name=benchmark.get("job_name"),
        namespace=test_namespace,
        image=benchmark.get("image"),
        timeout=benchmark.get("timeout_seconds"),
        pvc_size=benchmark.get("pvc_size"),
        pvc_storage_class=benchmark.get("pvc_storage_class"),
        guidellm_args=guidellm_args,
        config_path=config_path,
        fs_group=benchmark.get("fs_group"),
        use_pvc=benchmark.get("use_pvc"),
    )


def _deploy_sim_llmisvc(test_namespace, gateway_cfg, with_router):
    """Deploy sim LLMInferenceService with or without router/gateway."""
    from projects.kserve.toolbox.deploy_sim_llmisvc import main as deploy_sim_llmisvc_command

    if with_router:
        return deploy_sim_llmisvc_command.run(
            namespace=test_namespace,
            name=SIM_LLMISVC_NAME,
            gateway_name=gateway_cfg["name"],
            gateway_namespace=gateway_cfg["namespace"],
            gateway_status_address_name=gateway_cfg["status_address_name"],
        )

    endpoint = deploy_sim_llmisvc_command.run(
        namespace=test_namespace,
        name=SIM_LLMISVC_NAME,
        gateway_name="",
        gateway_namespace="",
        gateway_status_address_name=None,
        skip_router=True,
    )
    return endpoint.replace("https://", "http://", 1)


def _build_gateway_endpoint(gateway_cfg, test_namespace):
    """Construct the gateway internal URL."""
    return (
        f"http://{gateway_cfg['name']}-{gateway_cfg['gateway_class_name']}"
        f".{gateway_cfg['namespace']}.svc.cluster.local"
        f"/{test_namespace}/{SIM_LLMISVC_NAME}"
    )


def _smoke_test(test_namespace, endpoint_url):
    from projects.guidellm.toolbox.run_smoke_request import main as run_smoke_request_command

    smoke_cfg = config.project.get_config("workloads.smoke_request")
    run_smoke_request_command.run(
        namespace=test_namespace,
        endpoint_url=endpoint_url,
        served_model_name=SIM_MODEL_NAME,
        prompt=smoke_cfg["prompt"],
        max_tokens=smoke_cfg["max_tokens"],
        temperature=smoke_cfg["temperature"],
    )


def _ensure_gateway(gateway_cfg):
    from projects.llm_d.toolbox.ensure_gateway import main as ensure_gateway_command

    ensure_gateway_command.run(
        config_dir="",
        namespace=gateway_cfg["namespace"],
        name=gateway_cfg["name"],
        gateway_class_name=gateway_cfg["gateway_class_name"],
        status_address_name=gateway_cfg["status_address_name"],
        create_if_missing=gateway_cfg["create_if_missing"],
    )


def _deploy_praxis(test_namespace, model_endpoint):
    praxis_cfg = config.project.get_config("platform.praxis")
    deploy_praxis_command.run(
        namespace=test_namespace,
        model_endpoint=model_endpoint,
        praxis_version=praxis_cfg["version"],
        replicas=praxis_cfg["replicas"],
    )


def _prep_direct(test_namespace, gateway_cfg):
    endpoint = _deploy_sim_llmisvc(test_namespace, gateway_cfg, with_router=False)
    logger.info("[direct] endpoint: %s", endpoint)
    _smoke_test(test_namespace, endpoint)
    return endpoint


def _prep_gateway(test_namespace, gateway_cfg):
    _ensure_gateway(gateway_cfg)
    endpoint = _deploy_sim_llmisvc(test_namespace, gateway_cfg, with_router=True)
    logger.info("[gateway] endpoint: %s", endpoint)
    _smoke_test(test_namespace, endpoint)
    return endpoint


def _prep_praxis(test_namespace, gateway_cfg):
    direct_endpoint = _deploy_sim_llmisvc(test_namespace, gateway_cfg, with_router=False)
    logger.info("[praxis] model direct endpoint: %s", direct_endpoint)
    _smoke_test(test_namespace, direct_endpoint)

    _deploy_praxis(test_namespace, direct_endpoint)
    praxis_url = f"http://praxis.{test_namespace}.svc:8080"
    logger.info("[praxis] praxis endpoint: %s", praxis_url)
    _smoke_test(test_namespace, praxis_url)
    return praxis_url


def _prep_praxis_gateway(test_namespace, gateway_cfg):
    raise NotImplementedError("praxis-gateway deployment not yet implemented")


FLAVOR_PREP = {
    "direct": _prep_direct,
    "gateway": _prep_gateway,
    "praxis": _prep_praxis,
    "praxis-gateway": _prep_praxis_gateway,
}


def do_test_praxis(test_namespace: str):
    sim_cfg = config.project.get_config("platform.sim")
    gateway_cfg = sim_cfg["gateway"]

    flavors = config.project.get_config("test.flavors")
    for flavor in flavors:
        with env.NextArtifactDir(flavor):
            test_dir = env.ARTIFACT_DIR
            create_test_metadata(test_dir, {"flavor": flavor})

            prep_fn = FLAVOR_PREP.get(flavor)
            if prep_fn is None:
                raise ValueError(f"Unknown test flavor: {flavor!r}. Valid: {list(FLAVOR_PREP)}")

            logger.info("--- Running test flavor: %s ---", flavor)
            benchmark_endpoint = prep_fn(test_namespace, gateway_cfg)
            logger.info("[%s] benchmark endpoint: %s", flavor, benchmark_endpoint)

            # _run_guidellm(test_namespace, benchmark_endpoint)


def do_test():
    logger.info("=== AG Praxis Project Test Phase ===")

    test_namespace = config.project.get_config("test.namespace")

    with env.NextArtifactDir("ag_praxis_test_dir"):
        test_dir = create_custom_test_metadata()
        try:
            update_test_labels_with_timing(test_dir, "test", "start")

            do_test_praxis(test_namespace)
            # test_maas()

        except Exception as e:
            logger.exception("Test failed with exception")
            update_test_labels_with_status(test_dir, False, f"Test failed with exception: {str(e)}")

            raise
        finally:
            update_test_labels_with_timing(test_dir, "test", "end")

    update_test_labels_with_status(test_dir, True, "Test completed successfully")

    return 0


def fournos_resolve_hardware_request(hardware_spec: dict):
    """
    Resolve hardware requirements for FournosJob based on AG Praxis project configuration.

    This is a stub implementation. Update spec.hardware based on project configuration.

    Args:
        hardware_spec: The current spec.hardware dict from the FournosJob. This object should be updated.

    """
    logger.info("Hardware resolution: stub implementation - no changes made")

    # Stub implementation - could be extended to:
    # - Read hardware config from project configuration
    # - Set hardware requirements based on workload needs
    # - Handle different hardware profiles (GPU, CPU, memory requirements)
    # - Example: return {"gpu": {"type": "nvidia-tesla-v100", "count": 1}, "memory": "32Gi"}

    return hardware_spec
