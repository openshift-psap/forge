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
    return run_and_postprocess(do_test_praxis)


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


def _execute_benchmark(test_namespace, endpoint_url, benchmark, artifact_suffix):
    """Run a resolved benchmark config."""
    from projects.guidellm.library import benchconf as benchconf_lib
    from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
    from projects.guidellm.toolbox.run_guidellm_benchmark import (
        main as run_guidellm_benchmark_command,
    )

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
        artifact_dirname_suffix=artifact_suffix,
    )


def _run_guidellm_benchmark(test_namespace, endpoint_url, benchmark_key):
    """Resolve a named benchmark and run it."""
    benchmark = _resolve_benchmark_config(benchmark_key)
    _execute_benchmark(test_namespace, endpoint_url, benchmark, benchmark_key)


def _build_benchmark_config(payload_name, sweep_name):
    """Build a benchmark config from payload and sweep dimensions."""
    import copy

    workloads = copy.deepcopy(config.project.get_config("workloads"))

    payloads = workloads.get("payloads", {})
    payload = payloads.get(payload_name)
    if payload is None:
        raise ValueError(f"Unknown payload: {payload_name!r}. Valid: {list(payloads)}")

    sweeps = workloads.get("sweeps", {})
    sweep = sweeps.get(sweep_name)
    if sweep is None:
        raise ValueError(f"Unknown sweep: {sweep_name!r}. Valid: {list(sweeps)}")

    duration = workloads.get("duration", 180)

    benchmark = {}
    for key in (
        "job_name",
        "image",
        "pvc_size",
        "pvc_storage_class",
        "timeout_seconds",
        "fs_group",
        "use_pvc",
    ):
        if key in workloads:
            benchmark[key] = workloads[key]

    workload_args = dict(workloads.get("args", {}))
    workload_args.update(
        {
            "profile": "kind=concurrent,streams={rate}",
            "data": f"kind=synthetic_text,prompt_tokens={payload['prompt_tokens']},output_tokens={payload['output_tokens']}",
            "constraint": f"kind=max_duration,seconds={duration}",
        }
    )
    benchmark["args"] = workload_args
    benchmark["rate"] = sweep

    return benchmark


def _write_benchmark_dry_run(endpoint_url):
    """Write the benchmark configuration that would be started to a YAML file."""
    import yaml

    from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args

    payload_name = config.project.get_config("test.payload")
    sweep_name = config.project.get_config("test.sweep")
    benchmark = _build_benchmark_config(payload_name, sweep_name)

    dry_run = {
        "payload": payload_name,
        "sweep": sweep_name,
        "endpoint_url": endpoint_url,
        "guidellm_args": build_guidellm_args(benchmark),
        "benchmark": benchmark,
    }

    if config.project.get_config("test.run_warmup"):
        warmup_key = config.project.get_config("workloads.warmup_benchmark_key")
        if warmup_key:
            warmup = _resolve_benchmark_config(warmup_key)
            dry_run["warmup"] = {
                "benchmark_key": warmup_key,
                "guidellm_args": build_guidellm_args(warmup),
                "benchmark": warmup,
            }

    dest = env.ARTIFACT_DIR / "benchmark_dry_run.yaml"
    dest.write_text(yaml.safe_dump(dry_run, sort_keys=False))
    logger.info("Benchmark dry-run config written to %s", dest)


def _run_guidellm(test_namespace: str, endpoint_url: str):
    """Run the warmup (if enabled) then the main GuideLLM benchmark."""
    if config.project.get_config("test.run_warmup"):
        warmup_key = config.project.get_config("workloads.warmup_benchmark_key")
        if warmup_key:
            logger.info("Running warmup benchmark: %s", warmup_key)
            with env.NextArtifactDir("warmup"):
                _run_guidellm_benchmark(test_namespace, endpoint_url, warmup_key)

    payload_name = config.project.get_config("test.payload")
    sweep_name = config.project.get_config("test.sweep")
    logger.info("Running benchmark: payload=%s sweep=%s", payload_name, sweep_name)
    benchmark = _build_benchmark_config(payload_name, sweep_name)
    _execute_benchmark(test_namespace, endpoint_url, benchmark, f"{payload_name}_{sweep_name}")


def _deploy_sim_llmisvc(test_namespace):
    """Deploy sim LLMInferenceService without router."""
    from projects.kserve.toolbox.deploy_sim_llmisvc import main as deploy_sim_llmisvc_command

    sim_cfg = config.project.get_config("platform.sim")

    endpoint = deploy_sim_llmisvc_command.run(
        namespace=test_namespace,
        name=SIM_LLMISVC_NAME,
        gateway_name="",
        gateway_namespace="",
        gateway_status_address_name=None,
        skip_router=True,
        time_to_first_token=sim_cfg["time_to_first_token"],
        inter_token_latency=sim_cfg["inter_token_latency"],
        mode=sim_cfg["mode"],
        max_num_seqs=sim_cfg["max_num_seqs"],
    )
    return endpoint.replace("https://", "http://", 1)


def _build_gateway_endpoint(gateway_cfg, test_namespace):
    """Construct the gateway internal URL."""
    return (
        f"http://{gateway_cfg['name']}-{gateway_cfg['gateway_class_name']}"
        f".{gateway_cfg['namespace']}.svc.cluster.local"
        f"/{test_namespace}/{SIM_LLMISVC_NAME}"
    )


def _smoke_test(test_namespace, endpoint_url, suffix):
    from projects.guidellm.toolbox.run_smoke_request import main as run_smoke_request_command

    smoke_cfg = config.project.get_config("workloads.smoke_request")
    run_smoke_request_command.run(
        namespace=test_namespace,
        endpoint_url=endpoint_url,
        served_model_name=SIM_MODEL_NAME,
        prompt=smoke_cfg["prompt"],
        max_tokens=smoke_cfg["max_tokens"],
        temperature=smoke_cfg["temperature"],
        artifact_dirname_suffix=suffix,
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


def _prep_direct(test_namespace, gateway_cfg, sim_endpoint):
    _smoke_test(test_namespace, sim_endpoint, "sim")
    return sim_endpoint


DIRECT_GATEWAY_HTTPROUTE_NAME = "direct-gateway-route"
SIM_LLMISVC_SVC_NAME = f"{SIM_LLMISVC_NAME}-kserve-workload-svc"


def _prep_direct_gateway(test_namespace, gateway_cfg, sim_endpoint):
    from projects.ag_praxis.toolbox.add_gateway_route import main as add_gateway_route_command

    logger.info("[direct-gateway] model direct endpoint: %s", sim_endpoint)
    _smoke_test(test_namespace, sim_endpoint, "sim")

    _ensure_gateway(gateway_cfg)
    gateway_endpoint = add_gateway_route_command.run(
        namespace=test_namespace,
        name=DIRECT_GATEWAY_HTTPROUTE_NAME,
        gateway_name=gateway_cfg["name"],
        gateway_namespace=gateway_cfg["namespace"],
        backend_service=SIM_LLMISVC_SVC_NAME,
        backend_port=8000,
    )
    logger.info("[direct-gateway] gateway endpoint: %s", gateway_endpoint)
    _smoke_test(test_namespace, gateway_endpoint, "gateway")
    return gateway_endpoint


def _prep_praxis(test_namespace, gateway_cfg, sim_endpoint):
    logger.info("[praxis] model direct endpoint: %s", sim_endpoint)
    _smoke_test(test_namespace, sim_endpoint, "sim")

    _deploy_praxis(test_namespace, sim_endpoint)
    praxis_url = f"http://praxis.{test_namespace}.svc:8080"
    logger.info("[praxis] praxis endpoint: %s", praxis_url)
    _smoke_test(test_namespace, praxis_url, "praxis")
    return praxis_url


PRAXIS_HTTPROUTE_NAME = "praxis-gateway-route"


def _add_gateway_route(test_namespace, gateway_cfg):
    from projects.ag_praxis.toolbox.add_gateway_route import main as add_gateway_route_command

    return add_gateway_route_command.run(
        namespace=test_namespace,
        name=PRAXIS_HTTPROUTE_NAME,
        gateway_name=gateway_cfg["name"],
        gateway_namespace=gateway_cfg["namespace"],
        backend_service="praxis",
        backend_port=8080,
    )


def _prep_praxis_gateway(test_namespace, gateway_cfg, sim_endpoint):
    logger.info("[praxis-gateway] model direct endpoint: %s", sim_endpoint)
    _smoke_test(test_namespace, sim_endpoint, "sim")

    _deploy_praxis(test_namespace, sim_endpoint)
    praxis_url = f"http://praxis.{test_namespace}.svc:8080"
    logger.info("[praxis-gateway] praxis endpoint: %s", praxis_url)
    _smoke_test(test_namespace, praxis_url, "praxis")

    _ensure_gateway(gateway_cfg)
    gateway_endpoint = _add_gateway_route(test_namespace, gateway_cfg)
    logger.info("[praxis-gateway] gateway endpoint: %s", gateway_endpoint)
    _smoke_test(test_namespace, gateway_endpoint, "gateway")
    return gateway_endpoint


def _cleanup_llmisvc(test_namespace):
    from projects.core.dsl.utils.k8s import best_effort_oc

    best_effort_oc("delete", "llminferenceservice", SIM_LLMISVC_NAME, "-n", test_namespace)


def _cleanup_praxis(test_namespace):
    from projects.ag_praxis.toolbox.teardown_praxis import main as teardown_praxis_command

    teardown_praxis_command.run(namespace=test_namespace)


def _cleanup_direct(test_namespace):
    pass


def _cleanup_direct_gateway(test_namespace):
    from projects.core.dsl.utils.k8s import best_effort_oc

    best_effort_oc("delete", "httproute", DIRECT_GATEWAY_HTTPROUTE_NAME, "-n", test_namespace)


def _cleanup_praxis_flavor(test_namespace):
    _cleanup_praxis(test_namespace)


def _cleanup_praxis_gateway(test_namespace):
    from projects.core.dsl.utils.k8s import best_effort_oc

    best_effort_oc("delete", "httproute", PRAXIS_HTTPROUTE_NAME, "-n", test_namespace)
    _cleanup_praxis(test_namespace)


def _endpoint_direct(test_namespace, gateway_cfg):
    return f"http://{SIM_LLMISVC_SVC_NAME}.{test_namespace}.svc.cluster.local"


def _endpoint_gateway(test_namespace, gateway_cfg):
    return (
        f"http://{gateway_cfg['name']}-{gateway_cfg['gateway_class_name']}"
        f".{gateway_cfg['namespace']}.svc.cluster.local"
    )


def _endpoint_praxis(test_namespace, gateway_cfg):
    return f"http://praxis.{test_namespace}.svc:8080"


FLAVORS = {
    "direct": (_prep_direct, _cleanup_direct, _endpoint_direct),
    "direct-gateway": (_prep_direct_gateway, _cleanup_direct_gateway, _endpoint_gateway),
    "praxis": (_prep_praxis, _cleanup_praxis_flavor, _endpoint_praxis),
    "praxis-gateway": (_prep_praxis_gateway, _cleanup_praxis_gateway, _endpoint_gateway),
}


def do_test_praxis():
    logger.info("=== AG Praxis Project Test Phase ===")
    from projects.core.orchestration.utils.k8s import ensure_namespace

    test_namespace = config.project.get_config("test.namespace")
    ensure_namespace(test_namespace)

    sim_cfg = config.project.get_config("platform.sim")
    gateway_cfg = sim_cfg["gateway"]

    sim_endpoint = _deploy_sim_llmisvc(test_namespace)
    logger.info("Sim LLMInferenceService endpoint: %s", sim_endpoint)

    try:
        _run_flavor_loop(test_namespace, gateway_cfg, sim_endpoint)
    finally:
        if not config.project.get_config("test.skip_cleanup"):
            _cleanup_llmisvc(test_namespace)


def _run_flavor_loop(test_namespace, gateway_cfg, sim_endpoint):
    flavors = config.project.get_config("test.flavors")
    continue_on_failure = config.project.get_config("test.continue_on_flavor_failure")
    failed_flavors = []

    for flavor in flavors:
        with env.NextArtifactDir(flavor):
            test_dir = env.ARTIFACT_DIR
            create_test_metadata(test_dir, {"flavor": flavor})

            flavor_fns = FLAVORS.get(flavor)
            if flavor_fns is None:
                raise ValueError(f"Unknown test flavor: {flavor!r}. Valid: {list(FLAVORS)}")

            prep_fn, cleanup_fn, endpoint_fn = flavor_fns
            logger.info("--- Running test flavor: %s ---", flavor)
            try:
                update_test_labels_with_timing(test_dir, "test", "start")

                if not config.project.get_config("test.skip_prepare"):
                    benchmark_endpoint = prep_fn(test_namespace, gateway_cfg, sim_endpoint)
                else:
                    benchmark_endpoint = endpoint_fn(test_namespace, gateway_cfg)

                logger.info("[%s] benchmark endpoint: %s", flavor, benchmark_endpoint)

                if config.project.get_config("test.run_benchmark"):
                    update_test_labels_with_timing(test_dir, "benchmark", "start")
                    _run_guidellm(test_namespace, benchmark_endpoint)
                    update_test_labels_with_timing(test_dir, "benchmark", "end")
                else:
                    _write_benchmark_dry_run(benchmark_endpoint)
            except Exception as e:
                logger.exception("Flavor %s failed", flavor)
                update_test_labels_with_status(test_dir, False, f"Flavor {flavor} failed: {e}")
                if not continue_on_failure:
                    raise
                failed_flavors.append(flavor)
                continue
            finally:
                update_test_labels_with_timing(test_dir, "test", "end")
                if not config.project.get_config("test.skip_cleanup"):
                    cleanup_fn(test_namespace)

            update_test_labels_with_status(
                test_dir, True, f"Flavor {flavor} completed successfully"
            )

    if failed_flavors:
        raise RuntimeError(f"Test flavors failed: {', '.join(failed_flavors)}")


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
