"""Export trace and experiment IDs for a standalone k6 read benchmark."""

from __future__ import annotations

import json
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils import write_json
from projects.mlflow.toolbox.seed_postgres.main import SEED_EXPERIMENT_PREFIX, get_seed_trace_data


@entrypoint
def run(
    *,
    namespace: str,
    experiment_prefix: str = SEED_EXPERIMENT_PREFIX,
    workspaces: str | list[str] | None = None,
) -> Path:
    """Export seeded trace pools as trace_data.json.

    Args:
        namespace: Kubernetes namespace that hosts PostgreSQL
        experiment_prefix: Prefix of the seeded MLflow experiments
        workspaces: JSON array of MLflow workspace names
    """
    if isinstance(workspaces, list):
        workspaces = json.dumps(workspaces)
    return execute_tasks(locals()).trace_data_path


@task
def export_trace_data(args, ctx):
    """Query seeded identifiers and write the k6 input file."""
    trace_data = get_seed_trace_data(
        namespace=args.namespace,
        experiment_prefix=args.experiment_prefix,
        workspaces=json.loads(args.workspaces) if args.workspaces else None,
    )
    if not trace_data["tenants"] or any(
        not tenant["trace_ids"] for tenant in trace_data["tenants"]
    ):
        raise RuntimeError("No seeded trace IDs found for one or more read tenants")
    ctx.trace_data_path = args.artifact_dir / "trace_data.json"
    write_json(ctx.trace_data_path, trace_data)
    return f"Saved trace pools in {ctx.trace_data_path}"


if __name__ == "__main__":
    run.main()
