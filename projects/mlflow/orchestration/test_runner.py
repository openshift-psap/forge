"""Execute and capture one MLflow benchmark matrix cell."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

from projects.cluster.library.prom.collection import capture_prometheus
from projects.core.library import env
from projects.core.library.postprocess import create_test_metadata
from projects.mlflow.orchestration.metrics.pg_stats import (
    capture as capture_pg_stats,
)
from projects.mlflow.orchestration.metrics.pg_stats import (
    reset_pg_stat_statements,
)
from projects.mlflow.orchestration.runtime_config import cfg
from projects.mlflow.orchestration.test_matrix import MatrixCell
from projects.mlflow.toolbox.capture_test_logs import main as capture_test_logs
from projects.mlflow.toolbox.export_trace_data import main as export_trace_data
from projects.mlflow.toolbox.k6 import K6Results, PhaseHooks, synchronize_phases, with_suffix
from projects.mlflow.toolbox.k6_metrics import (
    capture_read as capture_k6_read_metrics,
)
from projects.mlflow.toolbox.k6_metrics import (
    capture_write as capture_k6_write_metrics,
)
from projects.mlflow.toolbox.k6_metrics import (
    get_client_rps,
)
from projects.mlflow.toolbox.manage_placement.main import Placement
from projects.mlflow.toolbox.manage_tenant_workspaces.main import workspaces
from projects.mlflow.toolbox.run_k6_read.main import run as run_k6_read
from projects.mlflow.toolbox.run_k6_write.main import run as run_k6_write

logger = logging.getLogger(__name__)


def run_one_test(
    *,
    case: MatrixCell,
    traces_per_experiment: int | None,
    namespace: str,
    preset: str,
    job_name: str,
    placement: Placement,
) -> None:
    """Run one k6 workload and capture its metrics and artifacts."""
    workload = cfg.get_experiment_workload()
    create_test_metadata(
        env.ARTIFACT_DIR,
        {
            "preset": preset,
            "users": str(
                case.read_concurrency + case.write_concurrency
                if case.read_concurrency is not None and case.write_concurrency is not None
                else case.users
            ),
            "tenant_count": str(case.tenant_count),
            "users_per_tenant": str(case.users_per_tenant),
            "pg_size_gb": str(case.pg_size_gb),
            "mlflow_replicas": str(case.mlflow_replicas),
            "mlflow_workers": str(case.mlflow_workers),
            "warmup_duration_seconds": str(cfg.get_warmup_duration_seconds()),
            "duration_seconds": str(
                cfg.get_write_k6_duration_seconds()
                if workload == "write"
                else cfg.get_read_duration_seconds()
            ),
            "span_size_bytes": (
                str(case.span_size_bytes) if case.span_size_bytes is not None else ""
            ),
            "traces_per_experiment": (
                str(traces_per_experiment) if traces_per_experiment is not None else ""
            ),
            "experiments_per_test": (
                str(case.experiments_per_test) if case.experiments_per_test is not None else ""
            ),
            "seed_traces_per_experiment": str(case.seed_traces_per_experiment),
            "workload": workload,
            "read_concurrency": str(case.read_concurrency or ""),
            "write_concurrency": str(case.write_concurrency or ""),
            "read_users_per_tenant": str(
                case.read_concurrency // case.tenant_count
                if case.read_concurrency is not None
                else ""
            ),
            "write_users_per_tenant": str(
                case.write_concurrency // case.tenant_count
                if case.write_concurrency is not None
                else ""
            ),
            **placement.metadata(),
        },
    )

    test_start_time: datetime | None = None
    try:
        is_read = workload in {"read", "mixed"}
        trace_data_path = (
            _prepare_read_trace_data(namespace, tenant_count=case.tenant_count) if is_read else None
        )
        if workload == "mixed":
            if trace_data_path is None:
                raise ValueError("trace_data_path is required for a mixed benchmark")
            read_results, write_results = _run_mixed_test(
                case=case,
                traces_per_experiment=traces_per_experiment,
                namespace=namespace,
                job_name=job_name,
                trace_data_path=trace_data_path,
                placement=placement,
            )
            test_start_time, test_end_time = _measurement_window(read_results, write_results)
            read_job_name = with_suffix(job_name, "-read")
            write_job_name = with_suffix(job_name, "-write")
            capture_k6_read_metrics(
                results=read_results,
                output_dir=env.ARTIFACT_DIR,
                job_name=read_job_name,
                operation=cfg.get_read_operation(),
            )
            capture_k6_write_metrics(
                results=write_results,
                output_dir=env.ARTIFACT_DIR,
                job_name=write_job_name,
            )
            client_rps = get_client_rps(read_results) + get_client_rps(write_results)
        else:
            with synchronize_phases(
                PhaseHooks(measurement=lambda: _reset_measurement_stats(namespace))
            ):
                results = _run_test(
                    case=case,
                    traces_per_experiment=traces_per_experiment,
                    namespace=namespace,
                    job_name=job_name,
                    trace_data_path=trace_data_path,
                    placement=placement,
                )
            test_start_time, test_end_time = _measurement_window(results)
            if workload == "read":
                capture_k6_read_metrics(
                    results=results,
                    output_dir=env.ARTIFACT_DIR,
                    job_name=job_name,
                    operation=cfg.get_read_operation(),
                )
            else:
                capture_k6_write_metrics(
                    results=results,
                    output_dir=env.ARTIFACT_DIR,
                    job_name=job_name,
                )
            client_rps = get_client_rps(results)

        logger.info("Measured k6 window: %s to %s", test_start_time, test_end_time)
        capture_pg_stats(namespace=namespace, output_dir=env.ARTIFACT_DIR)
        capture_start, capture_end = _prometheus_window(test_start_time, test_end_time)
        with env.NextArtifactDir("prometheus_metrics"):
            capture_prometheus(
                start_time=capture_start,
                end_time=capture_end,
                runtime_params={
                    "test_namespace": namespace,
                    "query_duration": f"{int((capture_end - capture_start).total_seconds())}s",
                    "client_rps": f"{client_rps:.6f}",
                },
            )
    finally:
        try:
            capture_test_logs.run(
                namespace=namespace,
                since_time=test_start_time,
                tenant_namespaces=workspaces(case.tenant_count),
                operator_namespace=cfg.get_platform_config()["rhoai"]["namespace"],
            )
        except Exception:
            logger.exception("Failed to capture MLflow test logs")


def _measurement_window(*results: K6Results) -> tuple[datetime, datetime]:
    """Use the actual scenario window, or the overlap for a mixed run."""
    starts, ends = [], []
    for result in results:
        if result.measurement_start_time is None or result.measurement_end_time is None:
            raise RuntimeError("Missing k6 measurement timestamps")
        starts.append(result.measurement_start_time)
        ends.append(result.measurement_end_time)
    start, end = max(starts), min(ends)
    if end <= start:
        raise RuntimeError("The k6 measurement windows do not overlap")
    return start, end


def _prometheus_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Round inward because the shared kube-burner collector uses Unix seconds."""
    first_second = start.replace(microsecond=0)
    if first_second < start:
        first_second += timedelta(seconds=1)
    last_second = end.replace(microsecond=0)
    if last_second <= first_second:
        raise RuntimeError("The k6 measurement window is too short for Prometheus capture")
    return first_second, last_second


def _reset_measurement_stats(namespace: str) -> None:
    if not reset_pg_stat_statements(namespace=namespace):
        raise RuntimeError("Could not reset PostgreSQL statistics before measurement")


def _prepare_read_trace_data(namespace: str, *, tenant_count: int) -> Path:
    """Write the seeded trace IDs that the read k6 script will replay."""
    return export_trace_data.run(
        namespace=namespace,
        experiment_prefix=cfg.get_read_experiment_prefix(),
        workspaces=workspaces(tenant_count),
    )


def _run_test(
    *,
    case: MatrixCell,
    traces_per_experiment: int | None,
    namespace: str,
    job_name: str,
    trace_data_path: Path | None,
    placement: Placement,
) -> K6Results:
    """Run the configured read or write k6 workload."""
    common = {
        "job_name": job_name,
        "namespace": namespace,
        "tracking_uri": cfg.get_mlflow_tracking_uri(),
        "users": case.users,
        "image": cfg.get_k6_image(),
    }

    if cfg.get_experiment_workload() == "read":
        if trace_data_path is None:
            raise ValueError("trace_data_path is required for a read benchmark")
        return run_k6_read(
            **common,
            tenant_count=case.tenant_count,
            users_per_tenant=case.users_per_tenant,
            duration_seconds=cfg.get_read_duration_seconds(),
            warmup_duration_seconds=cfg.get_warmup_duration_seconds(),
            operation=cfg.get_read_operation(),
            trace_data_path=trace_data_path,
            storage_class_name=placement.read_data_storage_class_name,
            node_selector=placement.node_selectors["k6"],
            pvc_selector=placement.read_data_pvc_selector,
            search_filter=cfg.get_read_search_filter(),
            search_order_by=cfg.get_read_search_order_by(),
            search_max_results=cfg.get_read_search_max_results(),
        )

    return run_k6_write(
        **common,
        tenant_count=case.tenant_count,
        users_per_tenant=case.users_per_tenant,
        duration_seconds=cfg.get_write_k6_duration_seconds(),
        warmup_duration_seconds=cfg.get_warmup_duration_seconds(),
        mode=cfg.get_write_k6_mode(),
        ramp_up_warmup=cfg.get_write_k6_ramp_up_warmup(),
        span_size_bytes=(
            case.span_size_bytes
            if case.span_size_bytes is not None
            else cfg.get_write_k6_span_size_bytes()
        ),
        spans_per_trace=cfg.get_write_k6_spans_per_trace(),
        traces_per_experiment=(
            traces_per_experiment
            if traces_per_experiment is not None
            else cfg.get_write_k6_traces_per_experiment()
        ),
        experiment_prefix=cfg.get_write_k6_experiment_prefix(),
        otlp_path=cfg.get_write_k6_otlp_path(),
        node_selector=placement.node_selectors["k6"],
    )


def _run_mixed_test(
    *,
    case: MatrixCell,
    traces_per_experiment: int | None,
    namespace: str,
    job_name: str,
    trace_data_path: Path,
    placement: Placement,
) -> tuple[K6Results, K6Results]:
    """Run read and write Jobs through matching warmup and measurement phases."""
    if case.read_concurrency is None or case.write_concurrency is None:
        raise ValueError("mixed workload cells require read and write concurrency")
    if traces_per_experiment is None:
        raise ValueError("mixed workload requires a write trace count per experiment")

    tenants = case.tenant_count
    read_job_name = with_suffix(job_name, "-read")
    write_job_name = with_suffix(job_name, "-write")
    read_warmup_job_name = with_suffix(job_name, "-read-warmup")
    write_warmup_job_name = with_suffix(job_name, "-write-warmup")
    warmup_barrier = Barrier(2, timeout=900)
    measurement_barrier = Barrier(
        2, action=lambda: _reset_measurement_stats(namespace), timeout=900
    )
    node_selector = placement.node_selectors["k6"]
    shared_duration = cfg.get_read_duration_seconds()
    shared_warmup = cfg.get_warmup_duration_seconds()

    def run_read() -> K6Results:
        return run_k6_read(
            job_name=read_job_name,
            namespace=namespace,
            tracking_uri=cfg.get_mlflow_tracking_uri(),
            users=case.read_concurrency,
            tenant_count=tenants,
            users_per_tenant=case.read_concurrency // tenants,
            duration_seconds=shared_duration,
            warmup_duration_seconds=shared_warmup,
            warmup_job_name=read_warmup_job_name,
            operation=cfg.get_read_operation(),
            trace_data_path=trace_data_path,
            image=cfg.get_k6_image(),
            storage_class_name=placement.read_data_storage_class_name,
            node_selector=node_selector,
            pvc_selector=placement.read_data_pvc_selector,
            search_filter=cfg.get_read_search_filter(),
            search_order_by=cfg.get_read_search_order_by(),
            search_max_results=cfg.get_read_search_max_results(),
        )

    def run_write() -> K6Results:
        return run_k6_write(
            job_name=write_job_name,
            namespace=namespace,
            tracking_uri=cfg.get_mlflow_tracking_uri(),
            users=case.write_concurrency,
            tenant_count=tenants,
            users_per_tenant=case.write_concurrency // tenants,
            duration_seconds=shared_duration,
            warmup_duration_seconds=shared_warmup,
            mode=cfg.get_write_k6_mode(),
            ramp_up_warmup=cfg.get_write_k6_ramp_up_warmup(),
            image=cfg.get_k6_image(),
            span_size_bytes=(
                case.span_size_bytes
                if case.span_size_bytes is not None
                else cfg.get_write_k6_span_size_bytes()
            ),
            spans_per_trace=cfg.get_write_k6_spans_per_trace(),
            traces_per_experiment=traces_per_experiment,
            experiment_prefix=cfg.get_write_k6_experiment_prefix(),
            otlp_path=cfg.get_write_k6_otlp_path(),
            warmup_job_name=write_warmup_job_name,
            node_selector=node_selector,
        )

    hooks = PhaseHooks(
        warmup=warmup_barrier.wait if shared_warmup else None,
        measurement=measurement_barrier.wait,
    )

    def run_synchronized(run_workload) -> K6Results:
        env.ensure_thread_artifact_dir()
        with synchronize_phases(hooks):
            try:
                return run_workload()
            except Exception:
                warmup_barrier.abort()
                measurement_barrier.abort()
                raise

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="mlflow-k6-mixed") as pool:
        read_future = pool.submit(run_synchronized, run_read)
        write_future = pool.submit(run_synchronized, run_write)
        results: dict[object, K6Results] = {}
        for future in as_completed((read_future, write_future)):
            try:
                results[future] = future.result()
            except Exception:
                from projects.core.dsl.utils.k8s import oc

                for name in (
                    read_job_name,
                    read_warmup_job_name,
                    write_job_name,
                    write_warmup_job_name,
                ):
                    oc(
                        "delete",
                        "job",
                        name,
                        "-n",
                        namespace,
                        "--ignore-not-found=true",
                        check=False,
                    )
                raise
    return results[read_future], results[write_future]
