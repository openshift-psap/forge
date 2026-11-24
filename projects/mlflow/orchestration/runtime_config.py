from __future__ import annotations

from pathlib import Path
from typing import Any

from projects.agentic_tools.base_runtime_config import BaseRuntimeConfig
from projects.core.library import config
from projects.mlflow.toolbox.seed_postgres.main import SEED_EXPERIMENT_PREFIX


class MLflowConfig(BaseRuntimeConfig):
    """Runtime configuration for MLflow tracing performance tests."""

    def __init__(self) -> None:
        super().__init__(
            orchestration_dir=Path(__file__).resolve().parent,
            namespace_prefix="mlflow-perf",
        )

    def get_namespace(self) -> str:
        override = config.project.get_config("runtime.namespace_override", None)
        if override:
            return override
        return config.project.get_config("runtime.namespace", "mlflow-perf-testing")

    def get_mlflow_tracking_uri(self) -> str:
        return config.project.get_config("infrastructure.mlflow_tracking_uri")

    def get_platform_config(self) -> dict[str, Any]:
        """Return the project-owned RHOAI installation configuration."""
        return config.project.get_config("platform", print=False)

    def get_experiment_concurrency(self) -> list[int]:
        raw = config.project.get_config("experiment.concurrency")
        return raw if isinstance(raw, list) else [raw]

    def get_experiment_pg_size_gb(self) -> list[int]:
        raw = config.project.get_config("experiment.pg_size_gb")
        return raw if isinstance(raw, list) else [raw]

    def get_mlflow_replica_counts(self) -> list[int]:
        raw = config.project.get_config("experiment.mlflow.replicas", 1)
        values = raw if isinstance(raw, list) else [raw]
        counts = [int(value) for value in values]
        if not counts or any(value <= 0 for value in counts):
            raise ValueError("experiment.mlflow.replicas values must be positive")
        return counts

    def get_mlflow_worker_counts(self) -> list[int]:
        raw = config.project.get_config("experiment.mlflow.workers", 4)
        values = raw if isinstance(raw, list) else [raw]
        counts = [int(value) for value in values]
        if not counts or any(value <= 0 for value in counts):
            raise ValueError("experiment.mlflow.workers values must be positive")
        return counts

    def get_mlflow_image(self) -> str:
        image = config.project.get_config("experiment.mlflow.image")
        if not isinstance(image, str) or not image.strip():
            raise ValueError("experiment.mlflow.image must be a non-empty string")
        return image.strip()

    def get_mlflow_image_pull_policy(self) -> str:
        policy = config.project.get_config("experiment.mlflow.image_pull_policy")
        if not isinstance(policy, str) or policy not in {"Always", "IfNotPresent", "Never"}:
            raise ValueError(
                "experiment.mlflow.image_pull_policy must be Always, IfNotPresent, or Never"
            )
        return policy

    def get_mlflow_pod_annotations(self) -> dict[str, str]:
        """Return optional annotations applied to each MLflow custom resource."""
        annotations = config.project.get_config("experiment.mlflow.pod_annotations", {})
        if not isinstance(annotations, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in annotations.items()
        ):
            raise ValueError(
                "experiment.mlflow.pod_annotations must map non-empty strings to non-empty strings"
            )
        return annotations

    def get_seed_traces_per_experiment(self) -> list[int]:
        raw = config.project.get_config("experiment.seed.traces_per_experiment", 1)
        values = raw if isinstance(raw, list) else [raw]
        if any(value <= 0 for value in values):
            raise ValueError("experiment.seed.traces_per_experiment values must be positive")
        return values

    def get_experiment_workload(self) -> str:
        workload = config.project.get_config("experiment.workload", "write")
        if workload not in {"read", "write", "mixed"}:
            raise ValueError("experiment.workload must be 'read', 'write', or 'mixed'")
        return workload

    def get_mixed_read_concurrencies(self) -> list[int]:
        raw = config.project.get_config("experiment.mixed_workload.read_concurrency", 8)
        return raw if isinstance(raw, list) else [raw]

    def get_mixed_write_concurrencies(self) -> list[int]:
        raw = config.project.get_config("experiment.mixed_workload.write_concurrency", 4)
        return raw if isinstance(raw, list) else [raw]

    def get_mixed_tenant_counts(self) -> list[int]:
        raw = config.project.get_config("experiment.mixed_workload.tenant_count", 1)
        return raw if isinstance(raw, list) else [raw]

    def get_write_k6_mode(self) -> str:
        return config.project.get_config("experiment.write_k6.mode", "span_warm_start")

    def get_write_k6_span_size_bytes(self) -> int:
        return int(config.project.get_config("experiment.write_k6.span_size_bytes", 102400))

    def get_write_k6_span_sizes_bytes(self) -> list[int]:
        raw = config.project.get_config("experiment.write_k6.span_size_bytes", 102400)
        values = raw if isinstance(raw, list) else [raw]
        return [int(value) for value in values]

    def get_write_k6_spans_per_trace(self) -> int:
        return int(config.project.get_config("experiment.write_k6.spans_per_trace", 5))

    def get_write_k6_tenant_counts(self) -> list[int]:
        raw = config.project.get_config("experiment.write_k6.tenant_count", 1)
        return raw if isinstance(raw, list) else [raw]

    def get_write_k6_traces_per_experiment(self) -> int:
        return int(config.project.get_config("experiment.write_k6.traces_per_experiment", 1))

    def get_write_k6_experiments_per_test(self) -> list[int | None]:
        raw = config.project.get_config("experiment.write_k6.experiments_per_test", None)
        if raw is None:
            return [None]
        values = raw if isinstance(raw, list) else [raw]
        return [int(value) for value in values]

    def get_write_k6_matrix_cells(self) -> list[dict[str, int | None]] | None:
        """Return explicit k6 write matrix cells, when a preset defines them."""
        raw = config.project.get_config("experiment.write_k6.matrix_cells", None)
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise ValueError("experiment.write_k6.matrix_cells must be a list")
        cells: list[dict[str, int | None]] = []
        for cell in raw:
            if not isinstance(cell, dict):
                raise ValueError("Each matrix_cells entry must be a mapping")
            if "users_per_tenant" not in cell and "users" not in cell:
                raise ValueError("Each matrix cell requires users_per_tenant")
            if "span_size_bytes" not in cell:
                raise ValueError("Each matrix cell requires span_size_bytes")
            cells.append(
                {
                    "users_per_tenant": int(cell.get("users_per_tenant", cell.get("users"))),
                    "tenant_count": int(cell.get("tenant_count", 1)),
                    "span_size_bytes": int(cell["span_size_bytes"]),
                    "experiments_per_test": (
                        None
                        if cell.get("experiments_per_test") is None
                        else int(cell["experiments_per_test"])
                    ),
                    "pg_size_gb": (
                        None if cell.get("pg_size_gb") is None else int(cell["pg_size_gb"])
                    ),
                    "seed_traces_per_experiment": (
                        None
                        if cell.get("seed_traces_per_experiment") is None
                        else cell["seed_traces_per_experiment"]
                    ),
                }
            )
        return cells

    def get_write_k6_experiment_prefix(self) -> str:
        return config.project.get_config("experiment.write_k6.experiment_prefix", "k6-write")

    def get_write_k6_otlp_path(self) -> str | None:
        """Return an optional absolute OTLP path override for version-specific routing."""
        path = config.project.get_config("experiment.write_k6.otlp_path", None)
        if path is None:
            return None
        if isinstance(path, str):
            path = path.strip()
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or not path.strip()
            or any(character.isspace() for character in path)
        ):
            raise ValueError("experiment.write_k6.otlp_path must be null or an absolute URL path")
        return path.rstrip("/") or "/"

    def get_write_k6_duration_seconds(self) -> int:
        return int(config.project.get_config("experiment.write_k6.duration_seconds", 60))

    def get_write_k6_ramp_up_warmup(self) -> bool:
        raw = config.project.get_config("experiment.write_k6.ramp_up_warmup", False)
        if not isinstance(raw, bool):
            raise ValueError("experiment.write_k6.ramp_up_warmup must be a boolean")
        return raw

    def get_read_experiment_prefix(self) -> str:
        """Prefix used to select the seeded trace pool."""
        return config.project.get_config(
            "experiment.read.target_experiment_prefix", SEED_EXPERIMENT_PREFIX
        )

    def get_read_tenant_counts(self) -> list[int]:
        raw = config.project.get_config("experiment.read.tenant_count", 1)
        return raw if isinstance(raw, list) else [raw]

    def get_read_search_max_results(self) -> int:
        return config.project.get_config("experiment.read.max_results")

    def get_read_search_filter(self) -> str:
        return config.project.get_config("experiment.read.filter")

    def get_read_search_order_by(self) -> str:
        return config.project.get_config("experiment.read.order_by")

    def get_read_operation(self) -> str:
        return config.project.get_config("experiment.read.operation")

    def get_k6_image(self) -> str:
        return config.project.get_config("infrastructure.k6.image", "grafana/k6:master")

    def get_read_duration_seconds(self) -> int:
        duration = config.project.get_config("experiment.duration_seconds")
        if type(duration) is not int or duration <= 0:
            raise ValueError("experiment.duration_seconds must be a positive integer")
        return duration

    def get_warmup_duration_seconds(self) -> int:
        """Duration of unmeasured traffic before a k6 benchmark."""
        duration = config.project.get_config("experiment.warmup_duration_seconds")
        if type(duration) is not int or duration < 0:
            raise ValueError("experiment.warmup_duration_seconds must be a non-negative integer")
        return duration


cfg = MLflowConfig()
