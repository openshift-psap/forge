import logging
import sys
from datetime import UTC, datetime

from projects.core.library import config, env
from projects.core.library.ci import ExitCategory
from projects.core.library.postprocess import (
    create_test_metadata,
    run_and_postprocess,
    update_test_labels_with_status,
    update_test_labels_with_timing,
)

logger = logging.getLogger(__name__)


def _sim_served_model_names():
    """Return the list of served model names from config."""
    sim_cfg = config.project.get_config("platform.sim")
    return sim_cfg.get("served_model_names", [sim_cfg["model_name"]])


def test():
    """Main test function that wraps do_test_praxis() with outcome postprocessing."""
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
        "replica_count",
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


def _execute_benchmark(
    test_namespace, endpoint_url, benchmark, artifact_suffix, api_key_file_path=None
):
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
        replica_count=benchmark.get("replica_count", 1),
        artifact_dirname_suffix=artifact_suffix,
        api_key_file_path=api_key_file_path,
        validate_backend=config.project.get_config("workloads.validate_backend"),
    )


def _run_guidellm_benchmark(test_namespace, endpoint_url, benchmark_key):
    """Resolve a named benchmark and run it."""

    with env.NextArtifactDir("benchmark"):
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
        "replica_count",
    ):
        if key in workloads:
            benchmark[key] = workloads[key]

    model_name = config.project.get_config("platform.sim.model_name")

    workload_args = dict(workloads.get("args", {}))
    workload_args.update(
        {
            "model": model_name,
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


def _run_guidellm(test_namespace: str, endpoint_url: str, api_key_file_path=None):
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

    rates = benchmark.pop("rate")
    replica_count = benchmark.get("replica_count", 1)

    for rate in rates:
        per_replica_rate = round(rate / replica_count)
        run_benchmark = dict(benchmark)
        run_benchmark["rate"] = per_replica_rate
        suffix = f"{payload_name}_{sweep_name}_rate{rate}"
        logger.info(
            "Running rate=%s (per_replica=%s, replicas=%s)", rate, per_replica_rate, replica_count
        )
        _execute_benchmark(
            test_namespace,
            endpoint_url,
            run_benchmark,
            suffix,
            api_key_file_path=api_key_file_path,
        )


def _smoke_test(test_namespace, endpoint_url, suffix, served_model_name, api_key_file_path=None):
    from projects.guidellm.toolbox.run_smoke_request import main as run_smoke_request_command

    smoke_cfg = config.project.get_config("workloads.smoke_request")
    kwargs = {}
    if api_key_file_path:
        kwargs["api_key_file_path"] = api_key_file_path

    run_smoke_request_command.run(
        namespace=test_namespace,
        endpoint_url=endpoint_url,
        served_model_name=served_model_name,
        prompt=smoke_cfg["prompt"],
        max_tokens=smoke_cfg["max_tokens"],
        temperature=smoke_cfg["temperature"],
        artifact_dirname_suffix=suffix,
        **kwargs,
    )


def _resolve_maas_gateway_url():
    """Resolve the MaaS gateway URL and internal service name.

    Returns (gateway_url, curl_connect_to):
        gateway_url: the external hostname-based URL (https://maas.apps.…)
        curl_connect_to: a --connect-to value mapping the hostname to the internal service
    """
    from projects.core.dsl.utils.k8s import oc

    maas_cfg = config.project.get_config("platform.maas")
    gw_name = maas_cfg["gateway"]["name"]
    gw_namespace = maas_cfg["gateway"]["namespace"]

    result = oc(
        "get",
        "gateway",
        gw_name,
        "-n",
        gw_namespace,
        "-o",
        "jsonpath={.spec.gatewayClassName} {.spec.listeners[0].hostname}",
        log_stdout=True,
    )
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        raise RuntimeError(
            f"Failed to resolve gatewayClassName/hostname for {gw_name} in {gw_namespace}: "
            f"got {result.stdout.strip()!r}"
        )
    gateway_class, hostname = parts

    svc_name = f"{gw_name}-{gateway_class}.{gw_namespace}.svc.cluster.local"
    gateway_url = f"https://{hostname}"
    curl_connect_to = f"{hostname}:443:{svc_name}:443"

    logger.info("MaaS gateway URL: %s (connect-to: %s)", gateway_url, curl_connect_to)
    return gateway_url, curl_connect_to


def _create_maas_api_key(namespace, maas_gateway_url, curl_connect_to, subscription_name):
    """Create an ephemeral MaaS API key via the maas-deployer pod."""
    from projects.ag_praxis.toolbox.create_maas_token import main as create_maas_token_command

    deployer_namespace = config.project.get_config("platform.maas.namespace")

    context = create_maas_token_command.run(
        namespace=namespace,
        gateway_url=maas_gateway_url,
        curl_connect_to=curl_connect_to,
        subscription_name=subscription_name,
        deployer_namespace=deployer_namespace,
    )

    return context.api_key_file_path


def _toggle_praxis_for_endpoint(ep_name, maas_cfg):
    """Toggle praxis activation based on the endpoint name."""
    from projects.ag_praxis.toolbox.enable_maas_praxis import main as enable_maas_praxis_command

    if ep_name == "maas/praxis":
        logger.info("Activating praxis for endpoint %s", ep_name)
        praxis_cfg = config.project.get_config("platform.praxis")
        enable_maas_praxis_command.run(
            activate=True,
            tenant_namespace=maas_cfg["tenant_namespace"],
            payload_processing_config=praxis_cfg.get("payload_processing"),
        )
    elif ep_name == "maas":
        logger.info("Deactivating praxis for endpoint %s", ep_name)
        enable_maas_praxis_command.run(
            activate=False,
            tenant_namespace=maas_cfg["tenant_namespace"],
        )


def _test_endpoint(
    ep_name,
    ep_url,
    api_key_path,
    test_namespace,
    maas_cfg,
    model_name,
    run_benchmark,
    completed_endpoints,
):
    from projects.cluster.library.prom.collection import capture_prometheus
    from projects.kserve.toolbox.capture_sim_state import (
        main as capture_sim_state_command,
    )

    _toggle_praxis_for_endpoint(ep_name, maas_cfg)

    _smoke_test(
        test_namespace,
        ep_url,
        ep_name,
        model_name,
        api_key_file_path=api_key_path,
    )

    if not run_benchmark:
        return

    benchmark_start = datetime.now(UTC)

    test_dir = env.ARTIFACT_DIR
    payload_name = config.project.get_config("test.payload")
    sweep_name = config.project.get_config("test.sweep")
    create_test_metadata(
        test_dir,
        {"endpoint": ep_name},
        kpi_labels={
            "model_name": model_name,
            "guidellm_loadshape": f"{payload_name}_{sweep_name}",
        },
    )
    update_test_labels_with_timing(test_dir, "benchmark", "start")

    primary_exc = None
    finalizer_exc = None

    def _run_finalizer(description, callback, **kwargs):
        nonlocal finalizer_exc
        try:
            callback(**kwargs)
        except Exception:
            if primary_exc is None:
                logger.exception("Finalizer failed: %s", description)
                finalizer_exc = finalizer_exc or sys.exc_info()
            else:
                logger.exception("Ignoring %s failure after primary test failure", description)

    try:
        _run_guidellm(test_namespace, ep_url, api_key_file_path=api_key_path)
    except Exception as e:
        primary_exc = sys.exc_info()
        update_test_labels_with_status(test_dir, False, str(e))
    finally:
        sim_name = maas_cfg["llmisvc_name"]
        _run_finalizer(
            "capturing sim state",
            capture_sim_state_command.run,
            namespace=test_namespace,
            name=sim_name,
        )

        benchmark_end = datetime.now(UTC)

        def _capture_prom():
            with env.NextArtifactDir("prometheus_metrics"):
                capture_prometheus(benchmark_start, benchmark_end)

        _run_finalizer("capturing Prometheus metrics", _capture_prom)

    if primary_exc is not None:
        raise primary_exc[1].with_traceback(primary_exc[2])

    if finalizer_exc is not None:
        raise finalizer_exc[1].with_traceback(finalizer_exc[2])

    update_test_labels_with_timing(test_dir, "benchmark", "end")
    update_test_labels_with_status(test_dir, True, f"Endpoint {ep_name} completed successfully")
    completed_endpoints.append(ep_name)


def _deploy_maas_sim(test_namespace, maas_cfg):
    """Deploy a sim for MaaS as an LLMInferenceService with TLS.

    Returns the internal SVC endpoint URL.
    """
    from projects.kserve.toolbox.deploy_sim_llmisvc import main as deploy_sim_llmisvc_command

    sim_cfg = config.project.get_config("platform.sim")
    maas_gw = maas_cfg["gateway"]

    deploy_sim_llmisvc_command.run(
        namespace=test_namespace,
        name=maas_cfg["llmisvc_name"],
        model_name=sim_cfg["model_name"],
        image=sim_cfg["image"],
        gateway_name=maas_gw["name"],
        gateway_namespace=maas_gw["namespace"],
        maas_gateway_label=maas_cfg["maas_gateway_label"],
        skip_router=False,
        max_num_seqs=sim_cfg["max_num_seqs"],
        time_to_first_token=sim_cfg["time_to_first_token"],
        inter_token_latency=sim_cfg["inter_token_latency"],
        mode=sim_cfg["mode"],
    )

    llmisvc_name = maas_cfg["llmisvc_name"]
    svc_endpoint = (
        f"https://{llmisvc_name}-kserve-workload-svc.{test_namespace}.svc.cluster.local:8000"
    )

    logger.info("LLMInferenceService SVC endpoint: %s", svc_endpoint)
    return svc_endpoint


def _register_maas_model(test_namespace, maas_cfg, model_name):
    """Register the sim LLMInferenceService with MaaS."""
    from projects.ag_praxis.toolbox.register_maas_model import main as register_maas_model_command

    maas_sim_name = maas_cfg["llmisvc_name"]

    register_maas_model_command.run(
        model_namespace=test_namespace,
        tenant_namespace=maas_cfg["tenant_namespace"],
        llmisvc_name=maas_sim_name,
        model_ref_kind="LLMInferenceService",
    )


def _cleanup_maas_sim(test_namespace, maas_cfg):
    """Unregister and delete the MaaS sim LLMInferenceService."""
    from projects.ag_praxis.toolbox.unregister_maas_model import (
        main as unregister_maas_model_command,
    )
    from projects.core.dsl.utils.k8s import best_effort_oc

    maas_sim_name = maas_cfg["llmisvc_name"]

    unregister_maas_model_command.run(
        model_namespace=test_namespace,
        tenant_namespace=maas_cfg["tenant_namespace"],
        llmisvc_name=maas_sim_name,
        model_ref_kind="LLMInferenceService",
    )

    best_effort_oc("delete", "llminferenceservice", maas_sim_name, "-n", test_namespace)


def _validate_benchmark_rates():
    """Validate that all sweep rates divide evenly by replica_count."""
    benchmark = _build_benchmark_config(
        config.project.get_config("test.payload"),
        config.project.get_config("test.sweep"),
    )
    replica_count = benchmark.get("replica_count", 1)
    for rate in benchmark.get("rate", []):
        per_replica_rate = rate / replica_count
        if abs(per_replica_rate - round(per_replica_rate)) > 0.01:
            raise ValueError(
                f"rate={rate} does not divide evenly by replica_count={replica_count} "
                f"(got {per_replica_rate})"
            )


def _prepare_test_endpoints(test_namespace, maas_cfg):
    """Deploy sim, register model, resolve gateway, create API key, and build endpoint list."""
    svc_endpoint = _deploy_maas_sim(test_namespace, maas_cfg)

    model_name = config.project.get_config("platform.sim.model_name")
    _register_maas_model(test_namespace, maas_cfg, model_name)

    maas_gateway_url, maas_curl_connect_to = _resolve_maas_gateway_url()
    llmisvc_name = maas_cfg["llmisvc_name"]
    subscription_name = f"{test_namespace}-{llmisvc_name}-subscription"
    api_key_file_path = _create_maas_api_key(
        test_namespace, maas_gateway_url, maas_curl_connect_to, subscription_name
    )
    maas_endpoint = f"{maas_gateway_url}/{test_namespace}/{llmisvc_name}"
    logger.info("MaaS external endpoint: %s", maas_endpoint)
    logger.info("SVC endpoint: %s", svc_endpoint)

    all_endpoints = [
        ("svc", svc_endpoint, None),
        ("maas", maas_endpoint, api_key_file_path),
        ("maas/praxis", maas_endpoint, api_key_file_path),
    ]
    selected = config.project.get_config("test.endpoints")
    endpoints = [(n, u, k) for n, u, k in all_endpoints if n in selected]

    return endpoints, model_name, maas_endpoint


def _run_endpoint_benchmarking(
    endpoints,
    test_namespace,
    maas_cfg,
    model_name,
    run_benchmark,
    completed_endpoints,
):
    """Run smoke tests and benchmarks for each endpoint."""

    for ep_name, ep_url, api_key_path in endpoints:
        with env.NextArtifactDir(ep_name.replace("/", "_")):
            _test_endpoint(
                ep_name,
                ep_url,
                api_key_path,
                test_namespace,
                maas_cfg,
                model_name,
                run_benchmark,
                completed_endpoints,
            )


def do_test_praxis():
    logger.info("=== AG Praxis Project Test Phase ===")
    from projects.cluster.library.prom.collection import (
        prepare_user_workload_monitoring,
        validate_user_workload_monitoring,
    )
    from projects.core.orchestration.utils.k8s import ensure_namespace

    if config.project.get_config("test.run_benchmark"):
        _validate_benchmark_rates()

    validate_user_workload_monitoring()
    prepare_user_workload_monitoring(during="test")

    test_namespace = config.project.get_config("test.namespace")
    ensure_namespace(test_namespace)

    maas_cfg = config.project.get_config("platform.maas")
    maas_gw = maas_cfg["gateway"]

    from projects.ag_praxis.orchestration.prepare_phase import _register_namespace_in_gateway

    _register_namespace_in_gateway(test_namespace, maas_gw["name"], maas_gw["namespace"])

    run_benchmark = config.project.get_config("test.run_benchmark")
    primary_exc = None
    completed_endpoints = []

    try:
        endpoints, model_name, maas_endpoint = _prepare_test_endpoints(test_namespace, maas_cfg)

        with env.NextArtifactDir("benchmarking"):
            _run_endpoint_benchmarking(
                endpoints,
                test_namespace,
                maas_cfg,
                model_name,
                run_benchmark,
                completed_endpoints,
            )

        if not run_benchmark:
            _write_benchmark_dry_run(maas_endpoint)

    except Exception:
        primary_exc = sys.exc_info()
        logger.exception("Test phase failed")

    finalizer_exc = _run_cleanup(test_namespace, maas_cfg, primary_exc)

    if primary_exc is not None:
        raise primary_exc[1].with_traceback(primary_exc[2])

    if finalizer_exc is not None:
        raise finalizer_exc[1].with_traceback(finalizer_exc[2])

    endpoint_names = ", ".join(completed_endpoints) if completed_endpoints else "none"
    reason = f"Endpoints benchmarked: {endpoint_names}"

    return 0, ExitCategory.SUCCESS, reason


def _run_cleanup(test_namespace, maas_cfg, primary_exc):
    """Run cleanup finalizers after test phase."""
    finalizer_exc = None

    if not config.project.get_config("test.skip_cleanup"):
        try:
            _cleanup_maas_sim(test_namespace=test_namespace, maas_cfg=maas_cfg)
        except Exception:
            if primary_exc is None:
                logger.exception("Finalizer failed: cleaning up MaaS sim")
                finalizer_exc = sys.exc_info()
            else:
                logger.exception("Ignoring cleanup failure after primary test failure")

    return finalizer_exc


def fournos_resolve_hardware_request(hardware_spec: dict):
    """
    Resolve hardware requirements for FournosJob based on AG Praxis project configuration.

    This is a stub implementation. Update spec.hardware based on project configuration.

    Args:
        hardware_spec: The current spec.hardware dict from the FournosJob. This object should be updated.

    """
    logger.info("Hardware resolution: stub implementation - no changes made")

    return hardware_spec
