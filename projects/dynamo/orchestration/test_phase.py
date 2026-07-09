from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

from projects.core.dsl import shell
from projects.core.dsl.utils import slugify_identifier
from projects.core.dsl.utils.k8s import oc, oc_get_json
from projects.core.library import env
from projects.core.library.postprocess import run_and_postprocess, write_test_labels
from projects.core.library.run import SignalInterrupt
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.dynamo.orchestration.prepare_phase import prepare_model_cache
from projects.dynamo.orchestration.render_graph_deployment import render_graph_deployment
from projects.dynamo.orchestration.utils import write_yaml
from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark import main as run_guidellm_benchmark_command
from projects.guidellm.toolbox.run_smoke_request import main as run_smoke_request_command

logger = logging.getLogger(__name__)


def create_test_labels() -> None:
    from projects.dynamo.orchestration import runtime_config

    model_name = runtime_config.get_model_name()
    deployment_profile = runtime_config.get_deployment_profile_name()
    benchmark_keys = runtime_config.get_benchmark_keys()

    labels = {
        "model_name": model_name,
        "deployment_profile": deployment_profile,
        "framework": "dynamo",
    }

    if benchmark_keys:
        labels["guidellm_loadshape"] = (
            benchmark_keys[0] if len(benchmark_keys) == 1 else benchmark_keys
        )

    write_test_labels(env.ARTIFACT_DIR, labels)
    logger.info("Created test labels: %s", labels)


def run() -> int:
    return run_and_postprocess(do_test)


def run_finalizers(
    endpoint_url: str | None,
    primary_exc: tuple[type[BaseException], BaseException, Any] | None,
    finalizer_exc: tuple[type[BaseException], BaseException, Any] | None,
) -> tuple[type[BaseException], BaseException, Any] | None:
    def _run_finalizer(description: str, callback, **kwargs):
        try:
            callback(**kwargs)
        except Exception:
            if primary_exc is None:
                logger.exception("Finalizer failed: %s", description)
                return finalizer_exc or sys.exc_info()
            logger.exception("Ignoring %s failure after primary test failure", description)
        return finalizer_exc

    from projects.dynamo.orchestration import runtime_config

    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    capture_namespace_events = platform["artifacts"]["capture_namespace_events"]

    finalizer_exc = _run_finalizer(
        "capture dynamo state",
        capture_dynamo_state,
    )
    finalizer_exc = _run_finalizer(
        "write endpoint URL",
        write_endpoint_url,
        artifact_dir=env.ARTIFACT_DIR,
        endpoint_url=endpoint_url,
    )
    finalizer_exc = _run_finalizer(
        "capture namespace events",
        capture_namespace_events_after_test,
        artifact_dir=env.ARTIFACT_DIR,
        namespace=namespace,
        capture_namespace_events=capture_namespace_events,
    )
    finalizer_exc = _run_finalizer(
        "cleanup runtime resources",
        cleanup_test_resources,
    )

    return primary_exc, finalizer_exc


def do_test() -> int:
    from projects.dynamo.orchestration import runtime_config

    namespace = runtime_config.get_namespace()

    ensure_namespace(
        namespace,
        labels={
            "app.kubernetes.io/managed-by": "forge",
            "forge.openshift.io/project": "dynamo",
        },
    )

    endpoint_url: str | None = None
    primary_exc: tuple[type[BaseException], BaseException, Any] | None = None
    finalizer_exc: tuple[type[BaseException], BaseException, Any] | None = None

    with env.NextArtifactDir("dynamo_test"):
        try:
            create_test_labels()

            endpoint_url = deploy_graph_deployment()

            if not endpoint_url:
                raise ValueError("Failed to discover endpoint URL from DynamoGraphDeployment")
            run_smoke_request(endpoint_url=endpoint_url)

            run_guidellm_benchmark(endpoint_url=endpoint_url)
        except Exception:
            primary_exc = sys.exc_info()
        except SignalInterrupt:
            primary_exc = sys.exc_info()
        finally:
            do_finalizers = True
            if primary_exc and isinstance(primary_exc[1], SignalInterrupt):
                logging.warning("Caught a SignalInterrupt, skipping the finalizers")
                do_finalizers = False

            if do_finalizers:
                primary_exc, finalizer_exc = run_finalizers(
                    endpoint_url, primary_exc, finalizer_exc
                )

    if primary_exc is not None:
        raise primary_exc[1].with_traceback(primary_exc[2])

    if finalizer_exc is not None:
        raise finalizer_exc[1].with_traceback(finalizer_exc[2])

    return 0


def deploy_graph_deployment() -> str:
    """Deploy DynamoGraphDeployment and return the endpoint URL."""
    logger.info("Starting DynamoGraphDeployment deployment")

    from projects.dynamo.orchestration import runtime_config

    namespace = runtime_config.get_namespace()

    _prepare_model_cache()

    manifest_path = _build_graph_deployment_manifest()

    logger.info("Applying DynamoGraphDeployment from manifest: %s", manifest_path)
    oc("apply", "-f", str(manifest_path), "-n", namespace, check=True)

    _wait_for_dynamo_ready(namespace=namespace)

    endpoint_url = _discover_endpoint(namespace=namespace)

    logger.info("DynamoGraphDeployment deployed, endpoint: %s", endpoint_url)
    return endpoint_url


def _prepare_model_cache() -> None:
    from projects.dynamo.orchestration import runtime_config

    model_name = runtime_config.get_model_name()
    logger.info("Preparing model cache for model: %s", model_name)
    prepare_model_cache()


def _build_graph_deployment_manifest() -> Path:
    from projects.dynamo.orchestration import runtime_config

    config_dir = runtime_config.get_config_dir()
    namespace = runtime_config.get_namespace()
    model_name = runtime_config.get_model_name()
    model_slug = runtime_config.get_model_slug(model_name)
    deployment_profile = runtime_config.get_deployment_profile()
    model_cache = runtime_config.get_model_cache_config()
    dynamo_config = runtime_config.get_dynamo_config()

    manifest = render_graph_deployment(
        config_dir=config_dir,
        namespace=namespace,
        model_name=model_name,
        model_slug=model_slug,
        deployment_profile=deployment_profile,
        model_cache=model_cache,
        dynamo_config=dynamo_config,
    )

    artifacts_dir = env.ARTIFACT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifacts_dir / "dynamo-graph-deployment.yaml"
    write_yaml(manifest_path, manifest)

    logger.info("Built DynamoGraphDeployment manifest: %s", manifest_path)
    return manifest_path


def _wait_for_dynamo_ready(
    *,
    namespace: str,
    timeout_seconds: int = 600,
    poll_interval: int = 10,
) -> None:
    """Wait for all Dynamo pods to be Running."""
    logger.info("Waiting for Dynamo pods to be ready in %s (timeout=%ds)", namespace, timeout_seconds)
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        result = oc(
            "get", "pods", "-n", namespace,
            "-l", "forge.openshift.io/project=dynamo",
            "-o", "jsonpath={.items[*].status.phase}",
            check=False,
        )
        if result.returncode != 0:
            time.sleep(poll_interval)
            continue

        pods_data = oc_get_json("pods", namespace=namespace, ignore_not_found=True)
        if not pods_data:
            time.sleep(poll_interval)
            continue

        items = pods_data.get("items", [])
        if not items:
            logger.info("No pods found yet, waiting...")
            time.sleep(poll_interval)
            continue

        all_ready = True
        for pod in items:
            phase = pod.get("status", {}).get("phase", "Unknown")
            name = pod.get("metadata", {}).get("name", "unknown")
            if phase not in ("Running", "Succeeded"):
                logger.info("Pod %s is %s", name, phase)
                all_ready = False

        if all_ready and items:
            logger.info("All %d Dynamo pods are ready", len(items))
            return

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Dynamo pods did not become ready within {timeout_seconds}s in namespace {namespace}"
    )


def _discover_endpoint(*, namespace: str) -> str:
    """Discover the inference endpoint URL from the Dynamo deployment."""
    svc_data = oc_get_json(
        "services", namespace=namespace,
        selector="app.kubernetes.io/managed-by=forge",
        ignore_not_found=True,
    )

    if svc_data:
        items = svc_data.get("items", [])
        for svc in items:
            ports = svc.get("spec", {}).get("ports", [])
            svc_name = svc.get("metadata", {}).get("name", "")
            for port in ports:
                if port.get("port") in (8000, 80):
                    return f"http://{svc_name}.{namespace}.svc.cluster.local:{port['port']}"

    route_data = oc_get_json("routes", namespace=namespace, ignore_not_found=True)
    if route_data:
        items = route_data.get("items", [])
        for route in items:
            host = route.get("spec", {}).get("host", "")
            if host:
                tls = route.get("spec", {}).get("tls")
                scheme = "https" if tls else "http"
                return f"{scheme}://{host}"

    raise RuntimeError(f"Could not discover Dynamo endpoint in namespace {namespace}")


def run_smoke_request(*, endpoint_url: str) -> dict[str, object]:
    from projects.dynamo.orchestration import runtime_config

    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    smoke = platform["smoke"]
    smoke_request = runtime_config.get_smoke_request()

    return run_smoke_request_command.run(
        namespace=namespace,
        endpoint_url=endpoint_url,
        pod_name=smoke["pod_name"],
        client_image=smoke["client_image"],
        endpoint_path=smoke["endpoint_path"],
        request_timeout_seconds=smoke["request_timeout_seconds"],
        served_model_name=runtime_config.get_served_model_name(),
        prompt=smoke_request["prompt"],
        max_tokens=smoke_request["max_tokens"],
        temperature=smoke_request["temperature"],
    )


def run_guidellm_benchmark(*, endpoint_url: str) -> None:
    from projects.dynamo.orchestration import runtime_config

    namespace = runtime_config.get_namespace()
    benchmark_configs = runtime_config.get_benchmark_configs()

    if not benchmark_configs:
        return

    for benchmark_key, benchmark in benchmark_configs:
        guidellm_args = build_guidellm_args(benchmark)
        if not any(arg.startswith("--processor=") for arg in guidellm_args):
            guidellm_args.append(f"--processor={runtime_config.get_model_name()}")
        artifact_name = f"benchmark_{slugify_identifier(benchmark_key, max_length=48)}"
        with env.NextArtifactDir(artifact_name):
            run_guidellm_benchmark_command.run(
                endpoint_url=endpoint_url,
                name=benchmark.get("job_name"),
                namespace=namespace,
                image=benchmark.get("image"),
                timeout=benchmark.get("timeout_seconds"),
                pvc_size=benchmark.get("pvc_size"),
                guidellm_args=guidellm_args,
            )


def capture_dynamo_state() -> None:
    from projects.dynamo.orchestration import runtime_config
    from projects.dynamo.toolbox.capture_dynamo_state.main import run as capture_state

    namespace = runtime_config.get_namespace()
    dynamo_config = runtime_config.get_dynamo_config()

    capture_state(
        artifact_dir=env.ARTIFACT_DIR,
        namespace=namespace,
        dynamo_namespace=dynamo_config["helm"]["namespace"],
    )


def write_endpoint_url(*, artifact_dir: Path, endpoint_url: str | None) -> None:
    if not endpoint_url:
        return

    endpoint_file = artifact_dir / "artifacts" / "endpoint.url"
    endpoint_file.parent.mkdir(parents=True, exist_ok=True)
    endpoint_file.write_text(f"{endpoint_url}\n", encoding="utf-8")


def cleanup_test_resources() -> None:
    from projects.dynamo.orchestration import runtime_config
    from projects.dynamo.toolbox.cleanup_dynamo_resources.main import run as cleanup

    namespace = runtime_config.get_namespace()
    benchmark_job_names = runtime_config.get_benchmark_job_names() or [None]

    for benchmark_job_name in benchmark_job_names:
        cleanup(
            namespace=namespace,
            benchmark_job_name=benchmark_job_name,
        )


def capture_namespace_events_after_test(
    *,
    artifact_dir: Path,
    namespace: str,
    capture_namespace_events: bool,
) -> None:
    if not capture_namespace_events:
        return

    shell.run(
        f"oc get events -n {namespace} --sort-by=.metadata.creationTimestamp",
        check=False,
        stdout_dest=artifact_dir / "artifacts" / "namespace.events.txt",
    )
