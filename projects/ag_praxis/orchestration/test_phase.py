import contextlib
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


def _configure_matrix_visualization(matrix_config: dict) -> None:
    """Populate caliper plugin_settings from matrix visualization config."""
    viz = matrix_config.get("visualization", {})
    if not viz:
        return

    config.project.set_config("caliper.postprocess.visualize.plugin_settings", dict(viz))


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
    test_namespace, endpoint_url, benchmark, artifact_suffix, api_key_file_path=None, ep_name=None
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

    job_name = benchmark.get("job_name")
    if ep_name:
        job_name = f"{job_name}-{ep_name.replace('/', '-')}"

    run_guidellm_benchmark_command.run(
        endpoint_url=endpoint_url,
        name=job_name,
        namespace=test_namespace,
        image=benchmark.get("image"),
        timeout=benchmark.get("timeout_seconds"),
        pvc_size=benchmark.get("pvc_size"),
        pvc_storage_class=benchmark.get("pvc_storage_class"),
        guidellm_args=guidellm_args,
        config_path=config_path,
        fs_group=benchmark.get("fs_group"),
        use_pvc=benchmark.get("use_pvc"),
        replica_count=_validated_replica_count(benchmark),
        artifact_dirname_suffix=artifact_suffix,
        api_key_file_path=api_key_file_path,
        validate_backend=config.project.get_config("workloads.validate_backend"),
        keep_full_benchmark_file=config.project.get_config("workloads.keep_full_benchmark_file"),
        drop_request_details=config.project.get_config("workloads.drop_request_details"),
    )


def _run_guidellm_warmup(
    test_namespace, endpoint_url, benchmark_key, ep_name=None, api_key_file_path=None
):
    """Resolve a named warmup benchmark and run it."""

    with env.NextArtifactDir("benchmark"):
        benchmark = _resolve_benchmark_config(benchmark_key)
        _execute_benchmark(
            test_namespace,
            endpoint_url,
            benchmark,
            benchmark_key,
            api_key_file_path=api_key_file_path,
            ep_name=ep_name,
        )


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

    # Number of requests each stream issues before the level completes. Unlike
    # a max_duration constraint, max_requests lets every request run fully
    # through, so no in-flight stream is cancelled at level end (cancellation
    # bursts trigger the 404 race in the sim's fasthttp server). The trailing
    # drain period is occluded from the metrics via the profile cooldown.
    requests_per_stream = payload.get(
        "requests_per_stream", workloads.get("requests_per_stream", 36)
    )

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
            "constraint": f"kind=max_requests,count={{{requests_per_stream}*rate}}",
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

    replica_count = _validated_replica_count(benchmark)
    replica_scaling = []
    for rate in benchmark.get("rate", []):
        per_replica_rate = round(rate / replica_count)
        replica_scaling.append(
            {"rate": rate, "replica_count": replica_count, "per_replica_rate": per_replica_rate}
        )

    dry_run = {
        "payload": payload_name,
        "sweep": sweep_name,
        "endpoint_url": endpoint_url,
        "replica_scaling": replica_scaling,
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


def _validated_replica_count(benchmark):
    """Return the replica count from the benchmark config.

    A missing, below-1, or non-integer value is a configuration error.
    """
    replica_count = benchmark.get("replica_count")
    if replica_count is None:
        raise ValueError("workloads.replica_count is not set: it must be an explicit integer >= 1")

    if not isinstance(replica_count, int) or replica_count < 1:
        raise ValueError(
            f"Invalid workloads.replica_count {replica_count!r}: must be an integer >= 1"
        )

    return replica_count


def _run_guidellm(test_namespace: str, endpoint_url: str, api_key_file_path=None, ep_name=None):
    """Run the warmup (if enabled) then the main GuideLLM benchmark."""
    if config.project.get_config("test.run_warmup"):
        warmup_key = config.project.get_config("workloads.warmup_benchmark_key")
        if warmup_key:
            logger.info("Running warmup benchmark: %s", warmup_key)
            with env.NextArtifactDir("warmup"):
                _run_guidellm_warmup(
                    test_namespace,
                    endpoint_url,
                    warmup_key,
                    ep_name=ep_name,
                    api_key_file_path=api_key_file_path,
                )

    payload_name = config.project.get_config("test.payload")
    sweep_name = config.project.get_config("test.sweep")
    logger.info("Running benchmark: payload=%s sweep=%s", payload_name, sweep_name)
    benchmark = _build_benchmark_config(payload_name, sweep_name)

    rates = benchmark.pop("rate")
    replica_count = _validated_replica_count(benchmark)

    for rate in rates:
        per_replica_rate = round(rate / replica_count)
        run_benchmark = dict(benchmark)
        run_benchmark["rate"] = per_replica_rate
        run_benchmark["replica_count"] = replica_count
        suffix = f"{payload_name}_{sweep_name}_rate{replica_count}x{per_replica_rate}"
        logger.info(
            "Running rate=%s (per_replica=%s, replicas=%s)", rate, per_replica_rate, replica_count
        )
        _execute_benchmark(
            test_namespace,
            endpoint_url,
            run_benchmark,
            suffix,
            api_key_file_path=api_key_file_path,
            ep_name=ep_name,
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
            ingress_namespace=maas_cfg["gateway"]["namespace"],
            gateway_name=maas_cfg["gateway"]["name"],
            payload_processing_config=praxis_cfg.get("payload_processing"),
            extproc_max_requests=praxis_cfg.get("extproc_max_requests"),
        )
    elif ep_name == "maas/ipp":
        logger.info("Deactivating praxis for endpoint %s", ep_name)
        enable_maas_praxis_command.run(
            activate=False,
            tenant_namespace=maas_cfg["tenant_namespace"],
            ingress_namespace=maas_cfg["gateway"]["namespace"],
            gateway_name=maas_cfg["gateway"]["name"],
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
    matrix_labels=None,
):
    from projects.ag_praxis.toolbox.capture_maas_praxis import main as capture_maas_praxis_command
    from projects.cluster.library.prom.collection import capture_prometheus
    from projects.cluster.toolbox.capture_cluster_state import main as capture_cluster_state_command
    from projects.kserve.toolbox.capture_llmisvc_state import main as capture_llmisvc_state_command

    benchmark_start = datetime.now(UTC)

    test_dir = env.ARTIFACT_DIR
    payload_name = config.project.get_config("test.payload")
    sweep_name = config.project.get_config("test.sweep")
    test_labels = {"endpoint": ep_name, **(matrix_labels or {})}
    create_test_metadata(
        test_dir,
        test_labels,
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
        _toggle_praxis_for_endpoint(ep_name, maas_cfg)

        if ep_name.startswith("maas"):
            _register_maas_model(test_namespace, maas_cfg, model_name)
            maas_gateway_url, maas_curl_connect_to = _resolve_maas_gateway_url()
            llmisvc_name = maas_cfg["llmisvc_name"]
            subscription_name = f"{test_namespace}-{llmisvc_name}-subscription"
            api_key_path = _create_maas_api_key(
                test_namespace, maas_gateway_url, maas_curl_connect_to, subscription_name
            )

        _smoke_test(
            test_namespace,
            ep_url,
            ep_name,
            model_name,
            api_key_file_path=api_key_path,
        )

        if run_benchmark:
            with env.NextArtifactDir("benchmarking"):
                _run_guidellm(
                    test_namespace, ep_url, api_key_file_path=api_key_path, ep_name=ep_name
                )
    except Exception as e:
        primary_exc = sys.exc_info()
        update_test_labels_with_status(test_dir, False, f"Endpoint test failed: {e}")
    finally:
        benchmark_end = update_test_labels_with_timing(test_dir, "benchmark", "end")

        def _capture_prom():
            with env.NextArtifactDir("prometheus_metrics"):
                capture_prometheus(benchmark_start, benchmark_end)

        _run_finalizer("capturing Prometheus metrics", _capture_prom)

        llmisvc_name = maas_cfg["llmisvc_name"]
        _run_finalizer(
            "capturing LLMInferenceService state",
            capture_llmisvc_state_command.run,
            llmisvc_name=llmisvc_name,
            namespace=test_namespace,
        )

        _run_finalizer(
            "capturing MaaS praxis state",
            capture_maas_praxis_command.run,
            ingress_namespace=maas_cfg["gateway"]["namespace"],
            tenant_namespace=maas_cfg["tenant_namespace"],
            gateway_name=maas_cfg["gateway"]["name"],
        )

        _run_finalizer("capturing cluster state", capture_cluster_state_command.run)

    if primary_exc is not None:
        # primary test status already tracked in the 'except' block
        raise primary_exc[1].with_traceback(primary_exc[2])

    if not run_benchmark:
        return

    if finalizer_exc is not None:
        update_test_labels_with_status(
            test_dir, False, f"Endpoint test finalizer failed: {finalizer_exc[1]}"
        )
        raise finalizer_exc[1].with_traceback(finalizer_exc[2])

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
        replicas=sim_cfg.get("replicas", 1),
    )

    llmisvc_name = maas_cfg["llmisvc_name"]
    svc_endpoint = (
        f"https://{llmisvc_name}-kserve-workload-svc.{test_namespace}.svc.cluster.local:8000"
    )

    logger.info("LLMInferenceService SVC endpoint: %s", svc_endpoint)
    return svc_endpoint


def _deploy_gw_sim(test_namespace):
    """Deploy a sim for default-gateway testing as an LLMInferenceService without MaaS.

    Returns the gateway endpoint URL.
    """
    from projects.kserve.toolbox.deploy_sim_llmisvc import main as deploy_sim_llmisvc_command

    gw_cfg = config.project.get_config("platform.default_gateway")
    sim_cfg = config.project.get_config("platform.sim")

    endpoint_url = deploy_sim_llmisvc_command.run(
        namespace=test_namespace,
        name=gw_cfg["llmisvc_name"],
        model_name=sim_cfg["model_name"],
        image=sim_cfg["image"],
        gateway_name=gw_cfg["name"],
        gateway_namespace=gw_cfg["namespace"],
        gateway_status_address_name=gw_cfg["status_address_name"],
        maas_gateway_label="",
        skip_router=False,
        max_num_seqs=sim_cfg["max_num_seqs"],
        time_to_first_token=sim_cfg["time_to_first_token"],
        inter_token_latency=sim_cfg["inter_token_latency"],
        mode=sim_cfg["mode"],
        replicas=sim_cfg.get("replicas", 1),
    )

    logger.info("GW LLMInferenceService endpoint: %s", endpoint_url)
    return endpoint_url


def _register_maas_model(test_namespace, maas_cfg, model_name):
    """Register the sim LLMInferenceService with MaaS."""
    from projects.ag_praxis.toolbox.register_maas_model import main as register_maas_model_command

    maas_sim_name = maas_cfg["llmisvc_name"]

    register_maas_model_command.run(
        model_namespace=test_namespace,
        tenant_namespace=maas_cfg["tenant_namespace"],
        llmisvc_name=maas_sim_name,
        model_ref_kind="LLMInferenceService",
        token_rate_limit=maas_cfg.get("token_rate_limit", 1_000_000_000),
        token_rate_limit_window=maas_cfg.get("token_rate_limit_window", "1m"),
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


def _cleanup_gw_sim(test_namespace):
    """Delete the default-gateway sim LLMInferenceService."""
    from projects.core.dsl.utils.k8s import best_effort_oc

    gw_cfg = config.project.get_config("platform.default_gateway")
    best_effort_oc("delete", "llminferenceservice", gw_cfg["llmisvc_name"], "-n", test_namespace)


def _validate_benchmark_rates():
    """Validate the rate -> replica scaling of every sweep level up front."""
    benchmark = _build_benchmark_config(
        config.project.get_config("test.payload"),
        config.project.get_config("test.sweep"),
    )

    replica_count = _validated_replica_count(benchmark)

    for rate in benchmark.get("rate", []):
        per_replica_rate = rate / replica_count
        if abs(per_replica_rate - round(per_replica_rate)) > 0.01:
            raise ValueError(
                f"rate={rate} does not divide evenly by replica_count={replica_count} "
                f"(got {per_replica_rate})"
            )


def _prepare_test_endpoints(test_namespace, maas_cfg):
    """Deploy sim(s), resolve gateway(s), and build endpoint list."""
    selected = config.project.get_config("test.endpoints")
    has_maas = any(ep.startswith("maas") for ep in selected)

    model_name = config.project.get_config("platform.sim.model_name")
    endpoint_map = {}
    maas_endpoint = None

    if has_maas or "svc" in selected:
        svc_endpoint = _deploy_maas_sim(test_namespace, maas_cfg)
        endpoint_map["svc"] = (svc_endpoint, None)
        logger.info("SVC endpoint: %s", svc_endpoint)

    if has_maas:
        maas_gateway_url, _ = _resolve_maas_gateway_url()
        llmisvc_name = maas_cfg["llmisvc_name"]
        maas_endpoint = f"{maas_gateway_url}/{test_namespace}/{llmisvc_name}"
        logger.info("MaaS external endpoint: %s", maas_endpoint)
        endpoint_map["maas/ipp"] = (maas_endpoint, None)
        endpoint_map["maas/praxis"] = (maas_endpoint, None)

    if "gw" in selected:
        gw_endpoint = _deploy_gw_sim(test_namespace)
        endpoint_map["gw"] = (gw_endpoint, None)

        if "svc" in selected and "svc" not in endpoint_map:
            gw_cfg = config.project.get_config("platform.default_gateway")
            gw_name = gw_cfg["llmisvc_name"]
            svc_endpoint = (
                f"https://{gw_name}-kserve-workload-svc.{test_namespace}.svc.cluster.local:8000"
            )
            endpoint_map["svc"] = (svc_endpoint, None)
            logger.info("SVC endpoint (from gw sim): %s", svc_endpoint)

    endpoints = [(name, *endpoint_map[name]) for name in selected if name in endpoint_map]

    return endpoints, model_name, maas_endpoint


def _run_endpoint_benchmarking(
    endpoints,
    test_namespace,
    maas_cfg,
    model_name,
    run_benchmark,
    completed_endpoints,
    matrix_labels=None,
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
                matrix_labels=matrix_labels,
            )


def _run_test_matrix(
    simulator_combos,
    deployment_combos,
    workload_combos,
    test_namespace,
    maas_cfg,
    run_benchmark,
    completed_endpoints,
):
    """Run the test matrix: simulator combos x deployment combos x workload combos x endpoints."""
    from projects.core.library.matrix import activate_combination

    for sim_combo in simulator_combos:
        sim_dir = (
            env.NextArtifactDir(sim_combo.artifact_dirname)
            if sim_combo.artifact_dirname
            else contextlib.nullcontext()
        )
        with activate_combination(sim_combo), sim_dir:
            for dep_combo in deployment_combos:
                dep_dir = (
                    env.NextArtifactDir(dep_combo.artifact_dirname)
                    if dep_combo.artifact_dirname
                    else contextlib.nullcontext()
                )
                with activate_combination(dep_combo), dep_dir:
                    endpoints, model_name, maas_endpoint = _prepare_test_endpoints(
                        test_namespace, maas_cfg
                    )
                    for wl_combo in workload_combos:
                        wl_dir = (
                            env.NextArtifactDir(wl_combo.artifact_dirname)
                            if wl_combo.artifact_dirname
                            else contextlib.nullcontext()
                        )
                        with activate_combination(wl_combo), wl_dir:
                            if run_benchmark:
                                _validate_benchmark_rates()

                            merged_labels = {
                                **sim_combo.labels,
                                **dep_combo.labels,
                                **wl_combo.labels,
                            }

                            _run_endpoint_benchmarking(
                                endpoints,
                                test_namespace,
                                maas_cfg,
                                model_name,
                                run_benchmark,
                                completed_endpoints,
                                matrix_labels=merged_labels,
                            )

    if not run_benchmark:
        _write_benchmark_dry_run(maas_endpoint)


def do_test_praxis():
    logger.info("=== AG Praxis Project Test Phase ===")
    from projects.cluster.library.prom.collection import (
        prepare_user_workload_monitoring,
        validate_user_workload_monitoring,
    )
    from projects.core.library.matrix import compute_matrix
    from projects.core.orchestration.utils.k8s import ensure_namespace

    validate_user_workload_monitoring()
    prepare_user_workload_monitoring(during="test")

    test_namespace = config.project.get_config("test.namespace")
    ensure_namespace(test_namespace)

    selected = config.project.get_config("test.endpoints")
    has_maas = any(ep.startswith("maas") for ep in selected)
    has_gw = "gw" in selected

    maas_cfg = config.project.get_config("platform.maas")

    if has_maas:
        maas_gw = maas_cfg["gateway"]

        from projects.ag_praxis.orchestration.prepare_phase import _register_namespace_in_gateway

        _register_namespace_in_gateway(test_namespace, maas_gw["name"], maas_gw["namespace"])

    if has_gw:
        from projects.ag_praxis.orchestration.prepare_phase import ensure_default_gateway

        ensure_default_gateway()

    run_benchmark = config.project.get_config("test.run_benchmark")

    matrix_cfg = config.project.get_config("test.matrix_benchmarking", default_value={})

    simulator_config = matrix_cfg.get("simulator_level", {})
    workload_config = matrix_cfg.get("workload_level", {})
    deployment_config = matrix_cfg.get("deployment_level", {})

    simulator_combos = compute_matrix(simulator_config)
    workload_combos = compute_matrix(workload_config)
    deployment_combos = compute_matrix(deployment_config)

    matrix_enabled = (
        simulator_config.get("enabled")
        or workload_config.get("enabled")
        or deployment_config.get("enabled")
    )
    if matrix_enabled:
        _configure_matrix_visualization(matrix_cfg)

    primary_exc = None
    completed_endpoints = []

    try:
        with env.NextArtifactDir("test"):
            _run_test_matrix(
                simulator_combos,
                deployment_combos,
                workload_combos,
                test_namespace,
                maas_cfg,
                run_benchmark,
                completed_endpoints,
            )

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
    from projects.kserve.toolbox.capture_llmisvc_state import (
        main as capture_llmisvc_state_command,
    )

    selected = config.project.get_config("test.endpoints")
    has_maas = any(ep.startswith("maas") for ep in selected)
    has_gw = "gw" in selected

    finalizer_exc = None

    def _safe_finalizer(description, callback, **kwargs):
        nonlocal finalizer_exc
        try:
            callback(**kwargs)
        except Exception:
            if primary_exc is None:
                logger.exception("Finalizer failed: %s", description)
                finalizer_exc = finalizer_exc or sys.exc_info()
            else:
                logger.exception("Ignoring %s failure after primary test failure", description)

    if has_maas or "svc" in selected:
        llmisvc_name = maas_cfg["llmisvc_name"]
        try:
            capture_llmisvc_state_command.run(llmisvc_name=llmisvc_name, namespace=test_namespace)
        except Exception:
            logger.exception("Failed to capture MaaS LLMInferenceService state before cleanup")
            if primary_exc is None:
                finalizer_exc = sys.exc_info()

    if has_gw:
        gw_cfg = config.project.get_config("platform.default_gateway")
        try:
            capture_llmisvc_state_command.run(
                llmisvc_name=gw_cfg["llmisvc_name"], namespace=test_namespace
            )
        except Exception:
            logger.exception("Failed to capture GW LLMInferenceService state before cleanup")
            if primary_exc is None:
                finalizer_exc = finalizer_exc or sys.exc_info()

    if not config.project.get_config("test.skip_cleanup"):
        if has_maas or "svc" in selected:
            _safe_finalizer(
                "cleaning up MaaS sim",
                _cleanup_maas_sim,
                test_namespace=test_namespace,
                maas_cfg=maas_cfg,
            )
        if has_gw:
            _safe_finalizer(
                "cleaning up GW sim",
                _cleanup_gw_sim,
                test_namespace=test_namespace,
            )

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
