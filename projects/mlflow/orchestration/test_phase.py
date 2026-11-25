"""Run the configured MLflow tracing benchmark matrix."""

from __future__ import annotations

import logging

from projects.cluster.library.prom.collection import (
    prepare_user_workload_monitoring,
    validate_user_workload_monitoring,
)
from projects.core.dsl.utils import write_json
from projects.core.library import env
from projects.core.library.postprocess import run_and_postprocess
from projects.mlflow.orchestration.runtime_config import cfg
from projects.mlflow.orchestration.test_matrix import MatrixCell, build_test_matrix, job_name
from projects.mlflow.orchestration.test_runner import run_one_test
from projects.mlflow.toolbox.apply_infrastructure import main as apply_infra_mod
from projects.mlflow.toolbox.capture_test_logs import main as capture_test_logs
from projects.mlflow.toolbox.cleanup_test_resources import main as cleanup_infra_mod
from projects.mlflow.toolbox.manage_placement import main as manage_placement
from projects.mlflow.toolbox.manage_tenant_workspaces import main as manage_tenant_workspaces
from projects.mlflow.toolbox.seed_postgres import main as seed_postgres

logger = logging.getLogger(__name__)


def run() -> int:
    """Run the test phase with standard Caliper postprocessing."""
    return run_and_postprocess(do_test)


def do_test() -> int:
    """Build the matrix, run each cell with isolated infrastructure, and summarize."""
    namespace = cfg.get_namespace()
    validate_user_workload_monitoring()
    prepare_user_workload_monitoring(during="test")

    preset = cfg.get_preset_name()
    concurrency = cfg.get_experiment_concurrency()
    workload = cfg.get_experiment_workload()
    is_mixed = workload == "mixed"
    is_write = workload in {"write", "mixed"}
    pg_sizes_gb = sorted(cfg.get_experiment_pg_size_gb())
    span_sizes = cfg.get_write_k6_span_sizes_bytes() if is_write else [None]
    tenant_counts = (
        cfg.get_mixed_tenant_counts()
        if is_mixed
        else (cfg.get_write_k6_tenant_counts() if is_write else cfg.get_read_tenant_counts())
    )
    experiment_counts = cfg.get_write_k6_experiments_per_test() if is_write else [None]
    explicit_cells = cfg.get_write_k6_matrix_cells() if workload == "write" else None
    cases = build_test_matrix(
        concurrency=concurrency,
        pg_sizes_gb=pg_sizes_gb,
        span_sizes=span_sizes,
        tenant_counts=tenant_counts,
        experiment_counts=experiment_counts,
        seed_trace_ratios=cfg.get_seed_traces_per_experiment(),
        explicit_cells=explicit_cells,
        read_concurrencies=cfg.get_mixed_read_concurrencies() if is_mixed else None,
        write_concurrencies=cfg.get_mixed_write_concurrencies() if is_mixed else None,
        mlflow_replicas=cfg.get_mlflow_replica_counts(),
        mlflow_workers=cfg.get_mlflow_worker_counts(),
    )
    if is_mixed and any(case.pg_size_gb <= 0 for case in cases):
        raise ValueError("mixed workloads require a positive pg_size_gb to seed read traces")
    _log_matrix(
        cases,
        pg_size_count=len(pg_sizes_gb),
        span_sizes=span_sizes,
        experiment_counts=experiment_counts,
    )

    workspace_state = None
    run_names: list[str] = []
    if cases:
        workspace_state = manage_tenant_workspaces.run(
            tenant_count=max(case.tenant_count for case in cases),
            k6_namespace=namespace,
        )
    try:
        for case in cases:
            name = job_name(case, preset)
            traces_per_experiment = _traces_per_experiment(case, is_write=is_write)
            if is_mixed:
                logger.info(
                    "[%s] tenants=%d read_concurrency=%d write_concurrency=%d "
                    "pg_size_gb=%d span_size=%s experiments_per_tenant=%s "
                    "seed_traces_per_experiment=%d mlflow_replicas=%d mlflow_workers=%d",
                    name,
                    case.tenant_count,
                    case.read_concurrency,
                    case.write_concurrency,
                    case.pg_size_gb,
                    case.span_size_bytes,
                    case.experiments_per_test,
                    case.seed_traces_per_experiment,
                    case.mlflow_replicas,
                    case.mlflow_workers,
                )
            else:
                logger.info(
                    "[%s] tenants=%d users_per_tenant=%d total_users=%d pg_size_gb=%d "
                    "span_size=%s experiments_per_tenant=%s seed_traces_per_experiment=%d "
                    "mlflow_replicas=%d mlflow_workers=%d",
                    name,
                    case.tenant_count,
                    case.users_per_tenant,
                    case.users,
                    case.pg_size_gb,
                    case.span_size_bytes,
                    case.experiments_per_test,
                    case.seed_traces_per_experiment,
                    case.mlflow_replicas,
                    case.mlflow_workers,
                )
            _run_matrix_cell(
                case=case,
                namespace=namespace,
                preset=preset,
                job_name=name,
                traces_per_experiment=traces_per_experiment,
                is_write=is_write,
            )
            run_names.append(name)
    finally:
        if workspace_state is not None:
            manage_tenant_workspaces.run(cleanup_from=workspace_state)

    _write_summary(
        preset=preset,
        workload=workload,
        concurrency=concurrency,
        cases=cases,
        pg_sizes_gb=pg_sizes_gb,
        span_sizes=span_sizes,
        experiment_counts=experiment_counts,
        explicit_cells=explicit_cells,
        run_names=run_names,
    )
    return 0


def _log_matrix(
    cases: list[MatrixCell],
    *,
    pg_size_count: int,
    span_sizes: list[int | None],
    experiment_counts: list[int | None],
) -> None:
    logger.info(
        "Test matrix: %d cells (pg_sizes=%d, users_per_tenant=%d, tenants=%d, "
        "span_sizes=%d, experiment_counts=%d, seed_trace_ratios=%s)",
        len(cases),
        pg_size_count,
        len({case.users_per_tenant for case in cases}),
        len({case.tenant_count for case in cases}),
        len(span_sizes),
        len(experiment_counts),
        sorted({case.seed_traces_per_experiment for case in cases}),
    )
    logger.info(
        "MLflow server matrix dimensions: replicas=%s workers=%s",
        sorted({case.mlflow_replicas for case in cases}),
        sorted({case.mlflow_workers for case in cases}),
    )


def _traces_per_experiment(case: MatrixCell, *, is_write: bool) -> int | None:
    if not is_write:
        return None
    if case.experiments_per_test is None:
        return cfg.get_write_k6_traces_per_experiment()
    write_users_per_tenant = (
        case.write_concurrency // case.tenant_count
        if case.write_concurrency is not None
        else case.users_per_tenant
    )
    if case.experiments_per_test <= 0 or write_users_per_tenant % case.experiments_per_test != 0:
        raise ValueError(
            f"experiments_per_test={case.experiments_per_test} must divide "
            f"write_users_per_tenant={write_users_per_tenant}"
        )
    return write_users_per_tenant // case.experiments_per_test


def _run_matrix_cell(
    *,
    case: MatrixCell,
    namespace: str,
    preset: str,
    job_name: str,
    traces_per_experiment: int | None,
    is_write: bool,
) -> None:
    reservation = None
    operator_namespace = cfg.get_platform_config()["rhoai"]["namespace"]
    try:
        cleanup_infra_mod.run(namespace=namespace, operator_namespace=operator_namespace)
        reservation = manage_placement.run(
            max_pg_size_gb=case.pg_size_gb,
            needs_read_data=cfg.get_experiment_workload() in {"read", "mixed"},
        )
        placement = manage_placement.Placement(**reservation.placement)
        logger.info("[%s] Deploying fresh infrastructure", job_name)
        apply_infra_mod.run(
            namespace=namespace,
            operator_namespace=operator_namespace,
            max_pg_size_gb=case.pg_size_gb,
            storage_class_name=placement.storage_class_name,
            node_selectors=placement.node_selectors,
            pvc_selectors=placement.pvc_selectors,
            mlflow_replicas=case.mlflow_replicas,
            mlflow_workers=case.mlflow_workers,
            mlflow_image=cfg.get_mlflow_image(),
            mlflow_image_pull_policy=cfg.get_mlflow_image_pull_policy(),
            mlflow_pod_annotations=cfg.get_mlflow_pod_annotations(),
        )
        if case.pg_size_gb > 0:
            logger.info("[%s] Seeding PostgreSQL to %d GiB", job_name, case.pg_size_gb)
            result = seed_postgres.run(
                namespace=namespace,
                pg_size_gb=case.pg_size_gb,
                workspaces=manage_tenant_workspaces.workspaces(case.tenant_count),
                traces_per_experiment=case.seed_traces_per_experiment,
            )
            logger.info("[%s] %s", job_name, result)

        with env.NextArtifactDir(job_name):
            run_one_test(
                case=case,
                traces_per_experiment=traces_per_experiment if is_write else None,
                namespace=namespace,
                preset=preset,
                job_name=job_name,
                placement=placement,
            )
    except Exception as error:
        try:
            diagnostics_dir = capture_test_logs.run(
                namespace=namespace,
                tenant_namespaces=manage_tenant_workspaces.workspaces(case.tenant_count),
                failure_type=type(error).__name__,
                operator_namespace=operator_namespace,
                artifact_dirname_suffix=f"{job_name}-failure",
            )
            logger.error(
                "[%s] Captured pre-cleanup failure diagnostics in %s",
                job_name,
                diagnostics_dir,
            )
        except Exception:
            logger.exception(
                "[%s] Failed to capture pre-cleanup failure diagnostics; "
                "preserving the original failure",
                job_name,
            )
        raise
    finally:
        logger.info("[%s] Removing cell infrastructure", job_name)
        try:
            cleanup_infra_mod.run(namespace=namespace, operator_namespace=operator_namespace)
        finally:
            if reservation is not None:
                manage_placement.run(release_from=str(reservation.placement_file))


def _write_summary(
    *,
    preset: str,
    workload: str,
    concurrency: list[int],
    cases: list[MatrixCell],
    pg_sizes_gb: list[int],
    span_sizes: list[int | None],
    experiment_counts: list[int | None],
    explicit_cells: list[dict[str, int | None]] | None,
    run_names: list[str],
) -> None:
    write_json(
        env.ARTIFACT_DIR / "test_summary.json",
        {
            "preset": preset,
            "workload": workload,
            "concurrency": concurrency,
            "tenant_counts": sorted({case.tenant_count for case in cases}),
            "pg_sizes_gb": pg_sizes_gb,
            "span_sizes_bytes": span_sizes,
            "experiments_per_test": experiment_counts,
            "seed_traces_per_experiment": sorted(
                {case.seed_traces_per_experiment for case in cases}
            ),
            "mlflow_replicas": sorted({case.mlflow_replicas for case in cases}),
            "mlflow_workers": sorted({case.mlflow_workers for case in cases}),
            "read_concurrencies": sorted(
                {case.read_concurrency for case in cases if case.read_concurrency is not None}
            ),
            "write_concurrencies": sorted(
                {case.write_concurrency for case in cases if case.write_concurrency is not None}
            ),
            "matrix_cells": explicit_cells,
            "run_names": run_names,
        },
    )
