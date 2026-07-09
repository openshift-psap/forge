"""
Test phase for MCP Gateway performance tests.

Iterates over the experiment matrix (servers x concurrency x target)
and executes a load test for each combination. Each test iteration
gets its own artifact directory (via ``NextArtifactDir``) containing
raw Locust output and a ``__test_labels__.yaml`` marker for Caliper
post-processing.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from projects.agentic_tools.locust.toolbox.run_distributed import main as run_locust
from projects.agentic_tools.locust.toolbox.run_distributed.main import LocustResults
from projects.agentic_tools.mcp.toolbox.deploy_mock_servers import main as deploy_mock_servers
from projects.caliper.prometheus_metrics.capture import capture_metrics
from projects.caliper.prometheus_metrics.config import MetricsCaptureConfig
from projects.core.dsl.utils import write_json
from projects.core.library import config, env
from projects.core.library.postprocess import run_and_postprocess, write_test_labels
from projects.mcp_gateway.orchestration.runtime_config import cfg
from projects.mcp_gateway.toolbox.apply_infrastructure import main as apply_infra

logger = logging.getLogger(__name__)


def run() -> int:
    """Main test function that wraps do_test() with Caliper postprocessing."""
    return run_and_postprocess(do_test)


def do_test() -> int:
    """Execute the experiment matrix: servers x concurrency x target."""
    namespace = cfg.get_namespace()
    preset = cfg.get_preset_name()
    mock_server = cfg.get_mock_server_key()
    mock_server_cfg = cfg.get_mock_server_config()
    version = cfg.get_deployed_version()

    servers = cfg.get_experiment_servers()
    concurrency = cfg.get_experiment_concurrency()
    targets = cfg.get_experiment_targets()
    tools_per_server = cfg.get_tools_per_server()
    scheduling = cfg.get_scheduling_config()
    warmup_seconds = cfg.get_warmup_seconds()
    metrics_cfg = MetricsCaptureConfig(**cfg.get_metrics_config())

    total = len(servers) * len(concurrency) * len(targets)
    logger.info(
        "Test matrix: %d servers x %d concurrency x %d targets = %d jobs",
        len(servers),
        len(concurrency),
        len(targets),
        total,
    )

    all_summaries: list[str] = []

    for num_servers in servers:
        for users in concurrency:
            for target in targets:
                job_name = f"mcp-{preset}-s{num_servers}-u{users}-{target}"[:63]

                logger.info(
                    "[%s] servers=%d users=%d target=%s",
                    job_name,
                    num_servers,
                    users,
                    target,
                )

                with env.NextArtifactDir(job_name):
                    run_one_test(
                        namespace=namespace,
                        preset=preset,
                        mock_server=mock_server,
                        mock_server_cfg=mock_server_cfg,
                        version=version,
                        num_servers=num_servers,
                        users=users,
                        target=target,
                        targets=[target],
                        tools_per_server=tools_per_server,
                        scheduling=scheduling,
                        warmup_seconds=warmup_seconds,
                        metrics_cfg=metrics_cfg,
                        job_name=job_name,
                    )

                all_summaries.append(job_name)

    summary: dict[str, Any] = {
        "preset": preset,
        "version": version,
        "servers": servers,
        "concurrency": concurrency,
        "targets": targets,
        "tools_per_server": tools_per_server,
        "run_names": all_summaries,
    }

    configured_version = config.project.get_config(
        "infrastructure.mcp_gateway_version", None, print=False, warn=False
    ) or os.environ.get("MCP_GATEWAY_VERSION")
    if configured_version and re.fullmatch(r"[0-9a-f]{40}", configured_version):
        summary["nightly"] = {
            "commit_sha": configured_version,
            "image_tag": f"sha-{configured_version}",
        }

    write_json(env.ARTIFACT_DIR / "test_summary.json", summary)

    return 0


def run_one_test(
    *,
    namespace: str,
    preset: str,
    mock_server: str,
    mock_server_cfg: dict[str, Any],
    version: str,
    num_servers: int,
    users: int,
    target: str,
    targets: list[str],
    tools_per_server: int,
    scheduling: dict[str, Any] | None,
    warmup_seconds: int,
    metrics_cfg: MetricsCaptureConfig,
    job_name: str,
) -> None:
    """Run a single test iteration inside a NextArtifactDir context."""
    write_test_labels(
        env.ARTIFACT_DIR,
        {
            "preset": preset,
            "target": target,
            "users": str(users),
            "num_servers": str(num_servers),
            "mock_server": mock_server,
            "mcp_gateway_version": version,
            "tools_per_server": str(tools_per_server),
        },
    )

    try:
        _deploy_servers(
            namespace=namespace,
            num_servers=num_servers,
            targets=targets,
            mock_server_cfg=mock_server_cfg,
            tools_per_server=tools_per_server,
            scheduling=scheduling,
        )

        run_start_time = datetime.now(UTC)
        _run_test(users=users, target=target, num_servers=num_servers)
        test_end_time = datetime.now(UTC)
        test_start_time = run_start_time + timedelta(seconds=warmup_seconds)

        _capture_pod_logs(namespace=namespace, run_dir=env.ARTIFACT_DIR)

        if metrics_cfg.enabled:
            metrics_out = env.ARTIFACT_DIR / "metrics" / "raw"
            metrics_out.mkdir(parents=True, exist_ok=True)
            capture_metrics(
                namespaces=metrics_cfg.namespaces,
                start_time=test_start_time,
                end_time=test_end_time,
                step_seconds=metrics_cfg.step_seconds,
                query_keys=metrics_cfg.query_keys or None,
                output_dir=metrics_out,
            )
    finally:
        _cleanup_locust_job(namespace=namespace, job_name=job_name)
        _cleanup_servers(
            namespace=namespace,
            num_servers=num_servers,
            mock_server=mock_server,
        )


# ---------------------------------------------------------------------------
# Deployment helpers
# ---------------------------------------------------------------------------


def _deploy_servers(
    *,
    namespace: str,
    num_servers: int,
    targets: list[str],
    mock_server_cfg: dict[str, Any],
    tools_per_server: int,
    scheduling: dict[str, Any] | None = None,
) -> None:
    """Deploy mock server(s) and gateway infrastructure."""
    image = mock_server_cfg.get("image", "quay.io/rh-ee-aharush/perf-mock-server:latest")
    sched = scheduling or {}

    deploy_mock_servers.run(
        namespace=namespace,
        count=num_servers,
        image=image,
        tools_per_server=tools_per_server,
        labels={"forge.openshift.io/project": "mcp_gateway"},
        node_selector=sched.get("node_selector"),
        tolerations=sched.get("tolerations"),
    )

    if "gateway" in targets:
        api_group = cfg.get_api_group()
        apply_infra.run(
            namespace=namespace,
            count=num_servers,
            api_group=api_group,
        )


def _cleanup_servers(*, namespace: str, num_servers: int, mock_server: str) -> None:
    """Remove mock servers and infrastructure after a server-level iteration."""
    from projects.core.dsl.utils.k8s import oc as run_oc

    deploy_mock_servers.cleanup_servers(namespace=namespace)

    api_group = cfg.get_api_group()
    from projects.agentic_tools.mcp.toolbox.deploy_mock_servers.main import MOCK_MCP_LABEL

    run_oc(
        "delete",
        f"mcpserverregistrations.{api_group},httproute",
        "-n",
        namespace,
        "-l",
        MOCK_MCP_LABEL,
        "--wait=false",
        "--ignore-not-found=true",
        check=False,
    )
    run_oc(
        "delete",
        "destinationrule",
        "-n",
        "istio-system",
        "-l",
        MOCK_MCP_LABEL,
        "--wait=false",
        "--ignore-not-found=true",
        check=False,
    )


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------


def _run_test(*, users: int, target: str, num_servers: int) -> LocustResults:
    """Run a single Locust test (artifacts saved by the run_distributed toolbox)."""
    locust_kwargs = cfg.build_locust_kwargs(
        users=users,
        target=target,
        num_servers=num_servers,
    )
    ctx = run_locust.run(**locust_kwargs, cleanup=False)
    return ctx.results


def _cleanup_locust_job(*, namespace: str, job_name: str) -> None:
    """Remove Locust master, workers, and headless service for a single test iteration."""
    from projects.core.dsl.utils.k8s import oc

    for resource in (
        f"job/{job_name}-master",
        f"job/{job_name}-workers",
        f"svc/{job_name}-master",
    ):
        oc("delete", resource, "-n", namespace, "--ignore-not-found=true", check=False)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _capture_pod_logs(*, namespace: str, run_dir: Path) -> None:
    """Capture logs from all pods in test namespace and gateway infrastructure namespaces."""
    from projects.core.dsl.utils.k8s import capture_pod_logs

    logs_dir = run_dir / "pod_logs"
    capture_pod_logs(namespace=namespace, output_dir=logs_dir)

    for gw_ns in ("mcp-system", "gateway-system"):
        gw_logs_dir = logs_dir / gw_ns
        capture_pod_logs(namespace=gw_ns, output_dir=gw_logs_dir)
