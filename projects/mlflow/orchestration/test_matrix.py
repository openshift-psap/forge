"""Build and name the configured MLflow benchmark matrix."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import product

from projects.core.dsl.utils import truncate_k8s_name


@dataclass(frozen=True)
class MatrixCell:
    """Inputs that identify one matrix cell."""

    pg_size_gb: int
    users_per_tenant: int
    tenant_count: int = 1
    span_size_bytes: int | None = None
    experiments_per_test: int | None = None
    seed_traces_per_experiment: int = 1
    read_concurrency: int | None = None
    write_concurrency: int | None = None
    mlflow_replicas: int = 1
    mlflow_workers: int = 4

    @property
    def users(self) -> int:
        """Total k6 VUs across all tenant workspaces."""
        return self.users_per_tenant * self.tenant_count


def build_test_matrix(
    *,
    concurrency: list[int],
    pg_sizes_gb: list[int],
    span_sizes: list[int | None],
    tenant_counts: list[int],
    experiment_counts: list[int | None],
    seed_trace_ratios: list[int],
    explicit_cells: list[dict[str, int | None]] | None,
    read_concurrencies: list[int] | None = None,
    write_concurrencies: list[int] | None = None,
    mlflow_replicas: list[int] | None = None,
    mlflow_workers: list[int] | None = None,
) -> list[MatrixCell]:
    """Expand configured dimensions or use explicitly listed workload cells."""
    replica_counts = [1] if mlflow_replicas is None else mlflow_replicas
    worker_counts = [4] if mlflow_workers is None else mlflow_workers
    if not replica_counts or any(value <= 0 for value in replica_counts):
        raise ValueError("MLflow replica counts must be positive")
    if not worker_counts or any(value <= 0 for value in worker_counts):
        raise ValueError("MLflow worker counts must be positive")
    if (read_concurrencies is None) != (write_concurrencies is None):
        raise ValueError("mixed matrices require both read and write concurrency")

    if explicit_cells is not None:
        if read_concurrencies is not None:
            raise ValueError("write_k6.matrix_cells cannot be used with mixed workloads")
        cases = _explicit_write_cases(explicit_cells, pg_sizes_gb, seed_trace_ratios)
    else:
        user_counts: Iterator[tuple[int, int | None]]
        if read_concurrencies is not None and write_concurrencies is not None:
            _validate_mixed_concurrency(read_concurrencies, write_concurrencies, tenant_counts)
            user_counts = product(read_concurrencies, write_concurrencies)
        else:
            user_counts = ((users, None) for users in concurrency)

        cases = _sweep_cases(
            pg_sizes_gb=pg_sizes_gb,
            span_sizes=span_sizes,
            experiment_counts=experiment_counts,
            user_counts=user_counts,
            tenant_counts=tenant_counts,
            seed_trace_ratios=seed_trace_ratios,
        )

    return [
        replace(case, mlflow_replicas=replicas, mlflow_workers=workers)
        for case, replicas, workers in product(cases, replica_counts, worker_counts)
    ]


def _validate_mixed_concurrency(
    read_concurrencies: list[int], write_concurrencies: list[int], tenant_counts: list[int]
) -> None:
    concurrency = read_concurrencies + write_concurrencies
    if any(users <= 0 for users in concurrency):
        raise ValueError("mixed read and write concurrency values must be positive")
    if any(tenants <= 0 for tenants in tenant_counts):
        raise ValueError("mixed workload tenant counts must be positive")
    if any(users % tenants for users, tenants in product(concurrency, tenant_counts)):
        raise ValueError("mixed read and write concurrency must divide evenly across tenants")


def _sweep_cases(
    *,
    pg_sizes_gb: list[int],
    span_sizes: list[int | None],
    experiment_counts: list[int | None],
    user_counts: Iterator[tuple[int, int | None]],
    tenant_counts: list[int],
    seed_trace_ratios: list[int],
) -> Iterator[MatrixCell]:
    """Expand independent axes, then apply the seed ratios for each database size."""
    combinations = product(pg_sizes_gb, span_sizes, experiment_counts, user_counts, tenant_counts)
    for pg_size, span_size, experiments, (users, write_users), tenants in combinations:
        # Single-workload concurrency is per tenant. Mixed concurrency is total.
        mixed = write_users is not None
        for ratio in _seed_ratios(pg_size, seed_trace_ratios):
            yield _validate_case(
                MatrixCell(
                    pg_size_gb=pg_size,
                    users_per_tenant=users // tenants if mixed else users,
                    tenant_count=tenants,
                    span_size_bytes=span_size,
                    experiments_per_test=experiments,
                    seed_traces_per_experiment=ratio,
                    read_concurrency=users if mixed else None,
                    write_concurrency=write_users,
                )
            )


def _explicit_write_cases(
    cells: list[dict[str, int | None]], pg_sizes_gb: list[int], seed_trace_ratios: list[int]
) -> Iterator[MatrixCell]:
    """Expand only the combinations specified by the write preset."""
    for cell in cells:
        pg_size = cell["pg_size_gb"]
        if pg_size is None:
            pg_size = pg_sizes_gb[0]
        seed_ratio = cell["seed_traces_per_experiment"]
        ratios = (
            [seed_ratio] if seed_ratio is not None else _seed_ratios(pg_size, seed_trace_ratios)
        )
        for ratio in ratios:
            yield _validate_case(
                MatrixCell(
                    pg_size_gb=pg_size,
                    users_per_tenant=cell["users_per_tenant"],
                    tenant_count=cell["tenant_count"],
                    span_size_bytes=cell["span_size_bytes"],
                    experiments_per_test=cell["experiments_per_test"],
                    seed_traces_per_experiment=ratio,
                )
            )


def _seed_ratios(pg_size_gb: int, configured: list[int]) -> list[int]:
    """Use one seed-ratio value for unseeded cases."""
    return configured if pg_size_gb > 0 else [1]


def _validate_case(case: MatrixCell) -> MatrixCell:
    if case.tenant_count <= 0 or case.users_per_tenant <= 0:
        raise ValueError("tenant_count and users_per_tenant must be positive")
    if case.seed_traces_per_experiment <= 0:
        raise ValueError("seed_traces_per_experiment must be positive")
    return case


def job_name(case: MatrixCell, preset: str) -> str:
    """Build a Kubernetes job name that distinguishes long matrix case names."""
    server = f"rep{case.mlflow_replicas}-wk{case.mlflow_workers}"
    seed = f"seed{case.seed_traces_per_experiment}"
    experiments = (
        str(case.experiments_per_test) if case.experiments_per_test is not None else "auto"
    )
    if case.read_concurrency is not None:
        name = (
            f"mlflow-mix-{server}-t{case.tenant_count}-r{case.read_concurrency}"
            f"-w{case.write_concurrency}-s{case.span_size_bytes}"
            f"-e{experiments}-pg{case.pg_size_gb}-{seed}-{preset}"
        )
        return truncate_k8s_name(name.rstrip("-"))

    tenants = f"t{case.tenant_count}-up{case.users_per_tenant}-" if case.tenant_count > 1 else ""
    prefix = f"mlflow-{server}-{tenants}{seed}"
    if case.span_size_bytes is None:
        name = f"{prefix}-{preset}-u{case.users}-pg{case.pg_size_gb}"
    else:
        name = (
            f"{prefix}-u{case.users}-s{case.span_size_bytes}-e{experiments}"
            f"-pg{case.pg_size_gb}-{preset}"
        )
    return truncate_k8s_name(name.rstrip("-"))
