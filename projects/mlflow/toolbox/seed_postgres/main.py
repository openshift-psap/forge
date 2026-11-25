"""Bulk-load cloned MLflow traces/spans directly into PostgreSQL.

Used by the test matrix to grow PostgreSQL to a target size before a test
tier runs. Writes through psql inside the PostgreSQL pod so the CI runner
never streams GB of row data -- only the ~1MB seed trace is transferred,
and Postgres expands it server-side via generate_series.

Growth is incremental: seed_traces() compares the current database size to
the target and only inserts the delta, so calling it again with a larger
pg_size_gb (e.g. after a previous call reached 10 GiB, call again with 50)
tops the database up instead of starting over. Calling it with a target at
or below the current size is a no-op.

The insert itself is parallelized across concurrent psql sessions. Each
session writes its disjoint trace-id range in bounded, transactional batches
using set-based INSERT ... SELECT ... FROM generate_series(...).
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger(__name__)

DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "data" / "seed_trace.json"

# Bulk-cloned seed traces are grouped into configurable experiments, named
# "<prefix>-<experiment_id>". Each experiment is assigned to one MLflow
# workspace; workspaces are assigned round-robin by experiment group.
# `namespace` identifies the Kubernetes namespace that hosts PostgreSQL.
SEED_EXPERIMENT_PREFIX = "trace-perf-seed"

_TRACE_ID_PH = "@@MLFLOW_TRACE_ID@@"
_TRACE_ID_B64_PH = "@@MLFLOW_TRACE_ID_B64@@"
_EXPERIMENT_ID_PH = "@@EXPERIMENT_ID@@"

_CALIBRATION_TRACES = 200
DEFAULT_PARALLELISM = 32
DEFAULT_BATCH_TRACES = 1_000

_SECONDARY_INDEXES = (
    ("index_trace_info_experiment_id_timestamp_ms", "trace_info", "(experiment_id, timestamp_ms)"),
    ("index_trace_info_timestamp_ms_request_id", "trace_info", "(timestamp_ms, request_id)"),
    ("index_spans_experiment_id_start_time", "spans", "(experiment_id, start_time_unix_nano)"),
    ("index_spans_experiment_id_status_type", "spans", "(experiment_id, status, type)"),
    ("index_spans_experiment_id_type_status", "spans", "(experiment_id, type, status)"),
    ("index_spans_experiment_id_duration", "spans", "(experiment_id, duration_ns)"),
    ("index_trace_tags_request_id", "trace_tags", "(request_id)"),
    ("index_trace_request_metadata_request_id", "trace_request_metadata", "(request_id)"),
)


@entrypoint
def run(
    *,
    namespace: str,
    pg_size_gb: int,
    traces_per_experiment: int = 1,
    workspaces: str | list[str] | None = None,
) -> str:
    """Seed PostgreSQL to a target size as a standalone toolbox.

    Args:
        namespace: Kubernetes namespace that hosts PostgreSQL
        pg_size_gb: Target database size in GiB
        traces_per_experiment: Number of traces to clone into each experiment
        workspaces: JSON array of MLflow workspace names
    """
    if isinstance(workspaces, list):
        workspaces = json.dumps(workspaces)
    return execute_tasks(locals()).result


@task
def seed_database(args, ctx):
    """Grow PostgreSQL to the requested target size."""
    ctx.result = seed_traces(
        namespace=args.namespace,
        pg_size_gb=args.pg_size_gb,
        workspaces=json.loads(args.workspaces) if args.workspaces else None,
        traces_per_experiment=args.traces_per_experiment,
    )
    return ctx.result


def seed_traces(
    *,
    namespace: str,
    pg_size_gb: int,
    seed_path: Path | None = None,
    parallelism: int = DEFAULT_PARALLELISM,
    batch_traces: int = DEFAULT_BATCH_TRACES,
    experiment_prefix: str = SEED_EXPERIMENT_PREFIX,
    workspaces: list[str] | None = None,
    traces_per_experiment: int = 1,
) -> str:
    """Grow PostgreSQL to pg_size_gb by cloning the seed trace, in parallel.

    Each group of `traces_per_experiment` cloned traces shares one experiment
    (named "<experiment_prefix>-<experiment_id>"). Experiment groups are
    distributed round-robin across `workspaces`. The Kubernetes `namespace`
    argument identifies the PostgreSQL pod namespace.

    Safe to call repeatedly with an increasing pg_size_gb across a pg_size
    tier sweep: only the delta above the current database size is inserted.
    """
    if pg_size_gb <= 0:
        return "pg_size_gb not set, skipping database seed"
    if traces_per_experiment <= 0:
        raise ValueError("traces_per_experiment must be positive")
    if parallelism <= 0:
        raise ValueError("parallelism must be positive")
    if batch_traces <= 0:
        raise ValueError("batch_traces must be positive")

    workspaces = list(dict.fromkeys(workspaces or [namespace]))
    if not workspaces or any(not workspace for workspace in workspaces):
        raise ValueError("workspaces must contain at least one non-empty workspace name")

    path = Path(seed_path) if seed_path else DEFAULT_SEED_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Seed trace not found: {path}")

    seed = json.loads(path.read_text(encoding="utf-8"))
    pod = _postgres_pod(namespace)
    target_bytes = int(pg_size_gb) * 1024 * 1024 * 1024

    current_bytes = _db_size_bytes(pod, namespace)
    if current_bytes >= target_bytes:
        return (
            f"Database already at {_pretty_size(pod, namespace, current_bytes)}, "
            f">= target {pg_size_gb} GiB, skipping seed"
        )

    _exec_sql(pod, namespace, _build_setup_sql(seed))
    next_id = int(_scalar(pod, namespace, _NEXT_ID_QUERY))
    group_start_id = next_id
    first_experiment_id = _next_experiment_id(pod, namespace)

    calibration_traces = _calibration_trace_count(traces_per_experiment)
    size_before_calibration = _db_size_bytes(pod, namespace)
    calibration_end = next_id + calibration_traces - 1
    _exec_sql(
        pod,
        namespace,
        _build_range_sql(
            seed,
            workspaces=workspaces,
            experiment_prefix=experiment_prefix,
            first_experiment_id=first_experiment_id,
            group_start_id=group_start_id,
            traces_per_experiment=traces_per_experiment,
            start_id=next_id,
            end_id=calibration_end,
        ),
    )
    size_after_calibration = _db_size_bytes(pod, namespace)
    bytes_per_trace = max(
        (size_after_calibration - size_before_calibration) // calibration_traces, 1
    )
    next_id = calibration_end + 1

    remaining_bytes = target_bytes - size_after_calibration
    total_needed = _round_up_to_multiple(
        max(0, -(-remaining_bytes // bytes_per_trace)), traces_per_experiment
    )

    workers = max(1, min(parallelism, total_needed))
    logger.info(
        "Seeding PostgreSQL toward %s GiB: %d bytes/trace, %d traces to add across %d workers",
        pg_size_gb,
        bytes_per_trace,
        total_needed,
        workers,
    )

    if total_needed > 0:
        ranges = _split_ranges(next_id, total_needed, workers)
        _run_parallel_ranges(
            pod,
            namespace,
            seed,
            workspaces=workspaces,
            experiment_prefix=experiment_prefix,
            first_experiment_id=first_experiment_id,
            group_start_id=group_start_id,
            traces_per_experiment=traces_per_experiment,
            ranges=ranges,
            batch_traces=batch_traces,
        )
        next_id += total_needed

    # Indexes are dropped for the bulk phase above, so the size estimate used
    # to size that phase does not include index/WAL/catalog overhead. Rebuild
    # indexes now, then top up against the *real* measured size -- the one
    # that includes everything -- until it actually reaches the target.
    _exec_sql(pod, namespace, _build_finalize_sql())
    _top_up_to_target(
        pod,
        namespace,
        seed,
        workspaces=workspaces,
        experiment_prefix=experiment_prefix,
        first_experiment_id=first_experiment_id,
        group_start_id=group_start_id,
        traces_per_experiment=traces_per_experiment,
        next_id=next_id,
        target_bytes=target_bytes,
        batch_traces=batch_traces,
    )
    # experiment_id values above were assigned explicitly,
    # bypassing the `experiments` table's SERIAL sequence entirely, so the
    # sequence itself is still sitting wherever it was before this call and
    # would hand out already-used ids to the next real app-created
    # experiment. Bump it past everything we just inserted.
    _exec_sql(pod, namespace, _BUMP_EXPERIMENT_SEQUENCE_SQL, "mlflow-seed-bump-sequence.sql")
    _exec_sql(pod, namespace, _ANALYZE_SQL, "mlflow-seed-final-analyze.sql")

    size = _pretty_size(pod, namespace, _db_size_bytes(pod, namespace))
    traces = _scalar(pod, namespace, "SELECT count(*) FROM trace_info;")
    experiments = _scalar(
        pod,
        namespace,
        f"SELECT count(*) FROM experiments WHERE name LIKE {_dollar('p7', experiment_prefix)} || '-%';",
    )
    return (
        f"Seeded {traces} traces across {experiments} experiments "
        f"(traces_per_experiment={traces_per_experiment}, workspaces={len(workspaces)}), "
        f"database size {size} (target {pg_size_gb} GiB)"
    )


def get_seed_trace_data(
    *,
    namespace: str,
    experiment_prefix: str = SEED_EXPERIMENT_PREFIX,
    workspaces: list[str] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Return seeded trace and experiment IDs for a read benchmark.

    This runs once after seeding, outside the measured k6 workload. The
    returned values are identifiers only; no trace payload is copied or logged.
    """
    pod = _postgres_pod(namespace)
    workspaces = list(dict.fromkeys(workspaces or [namespace]))
    tenants = []
    for index, workspace in enumerate(workspaces):
        workspace_lit = _dollar(f"workspace{index}", workspace)
        prefix_lit = _dollar(f"prefix{index}", experiment_prefix + "-%")
        trace_query = (
            "SELECT ti.request_id FROM trace_info ti "
            "JOIN experiments e ON e.experiment_id = ti.experiment_id "
            f"WHERE e.workspace = {workspace_lit} AND e.name LIKE {prefix_lit} "
            "ORDER BY ti.request_id;"
        )
        experiment_query = (
            "SELECT experiment_id FROM experiments "
            f"WHERE workspace = {workspace_lit} AND name LIKE {prefix_lit} "
            "ORDER BY experiment_id;"
        )
        tenants.append(
            {
                "workspace": workspace,
                "trace_ids": _query_lines(pod, namespace, trace_query),
                "experiment_ids": _query_lines(pod, namespace, experiment_query),
            }
        )
    return {
        "tenants": tenants,
    }


def _next_experiment_id(pod: str, namespace: str) -> int:
    """Return the first experiment ID not already present in the database."""
    return int(_scalar(pod, namespace, _NEXT_EXPERIMENT_ID_QUERY))


def _round_up_to_multiple(value: int, multiple: int) -> int:
    """Round a positive count up so it ends on an experiment-group boundary."""
    if value <= 0:
        return 0
    return -(-value // multiple) * multiple


def _calibration_trace_count(traces_per_experiment: int) -> int:
    """Keep calibration batches aligned to whole experiment groups."""
    return _round_up_to_multiple(_CALIBRATION_TRACES, traces_per_experiment)


def _top_up_to_target(
    pod: str,
    namespace: str,
    seed: dict,
    *,
    workspaces: list[str],
    experiment_prefix: str,
    first_experiment_id: int,
    group_start_id: int,
    traces_per_experiment: int,
    next_id: int,
    target_bytes: int,
    batch_traces: int,
    max_rounds: int = 5,
) -> None:
    """Close the gap between the real (indexed) database size and the target.

    Indexes are live at this point, so top-up inserts are slower per row than
    the bulk phase -- but the gap here is just the index/WAL/catalog overhead
    on top of the bulk data, a small fraction of the total, so a sequential
    (non-parallel) insert is fine. Each round re-measures actual
    pg_database_size() and recalibrates bytes-per-trace from a real insert
    (index maintenance cost included), so it converges even if the no-index
    estimate from the bulk phase was off.
    """
    calibration_traces = _calibration_trace_count(traces_per_experiment)
    for round_num in range(1, max_rounds + 1):
        current = _db_size_bytes(pod, namespace)
        remaining = target_bytes - current
        if remaining <= 0:
            return

        probe_end = next_id + calibration_traces - 1
        _exec_sql(
            pod,
            namespace,
            _build_range_sql(
                seed,
                workspaces=workspaces,
                experiment_prefix=experiment_prefix,
                first_experiment_id=first_experiment_id,
                group_start_id=group_start_id,
                traces_per_experiment=traces_per_experiment,
                start_id=next_id,
                end_id=probe_end,
            ),
            "mlflow-seed-topup-probe.sql",
        )
        next_id = probe_end + 1
        after_probe = _db_size_bytes(pod, namespace)
        bytes_per_trace = max((after_probe - current) // calibration_traces, 1)

        remaining = target_bytes - after_probe
        if remaining <= 0:
            return

        extra_needed = _round_up_to_multiple(
            -(-remaining // bytes_per_trace), traces_per_experiment
        )
        end_id = next_id + extra_needed - 1
        logger.info(
            "Top-up round %d: %s short of target, inserting %d more traces",
            round_num,
            _pretty_size(pod, namespace, remaining),
            extra_needed,
        )
        _run_parallel_ranges(
            pod,
            namespace,
            seed,
            workspaces=workspaces,
            experiment_prefix=experiment_prefix,
            first_experiment_id=first_experiment_id,
            group_start_id=group_start_id,
            traces_per_experiment=traces_per_experiment,
            ranges=[(next_id, end_id)],
            batch_traces=batch_traces,
            filename_prefix=f"mlflow-seed-topup-{round_num}",
        )
        next_id = end_id + 1

    final = _db_size_bytes(pod, namespace)
    if final < target_bytes:
        logger.warning(
            "Top-up did not fully converge after %d rounds: still %s short of target",
            max_rounds,
            _pretty_size(pod, namespace, target_bytes - final),
        )


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


def _split_ranges(start_id: int, total: int, workers: int) -> list[tuple[int, int]]:
    """Split [start_id, start_id + total) into `workers` disjoint contiguous ranges."""
    workers = max(1, workers)
    base, extra = divmod(total, workers)
    ranges = []
    cursor = start_id
    for i in range(workers):
        size = base + (1 if i < extra else 0)
        if size == 0:
            continue
        ranges.append((cursor, cursor + size - 1))
        cursor += size
    return ranges


def _run_parallel_ranges(
    pod: str,
    namespace: str,
    seed: dict,
    *,
    workspaces: list[str],
    experiment_prefix: str,
    first_experiment_id: int,
    group_start_id: int,
    traces_per_experiment: int,
    ranges: list[tuple[int, int]],
    batch_traces: int = DEFAULT_BATCH_TRACES,
    filename_prefix: str = "mlflow-seed-worker",
) -> None:
    """Run disjoint ranges as bounded transactions with per-batch progress."""

    def run_range(worker_id: int, start_id: int, end_id: int) -> None:
        batch_number = 0
        batch_start = start_id
        while batch_start <= end_id:
            batch_number += 1
            batch_end = min(batch_start + batch_traces - 1, end_id)
            started = time.monotonic()
            logger.info(
                "Seed %s %d batch %d starting: traces %d-%d (%d traces)",
                filename_prefix,
                worker_id,
                batch_number,
                batch_start,
                batch_end,
                batch_end - batch_start + 1,
            )
            _exec_sql(
                pod,
                namespace,
                _build_range_sql(
                    seed,
                    workspaces=workspaces,
                    experiment_prefix=experiment_prefix,
                    first_experiment_id=first_experiment_id,
                    group_start_id=group_start_id,
                    traces_per_experiment=traces_per_experiment,
                    start_id=batch_start,
                    end_id=batch_end,
                ),
                f"{filename_prefix}-{worker_id}-batch-{batch_number}.sql",
            )
            logger.info(
                "Seed %s %d batch %d completed in %.1fs",
                filename_prefix,
                worker_id,
                batch_number,
                time.monotonic() - started,
            )
            batch_start = batch_end + 1

    with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        futures = {
            pool.submit(
                run_range,
                i,
                start,
                end,
            ): (start, end)
            for i, (start, end) in enumerate(ranges)
        }
        for future in as_completed(futures):
            future.result()  # re-raise any worker failure


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------

_NEXT_ID_QUERY = (
    "SELECT COALESCE(MAX(('x' || substr(request_id, 20))::bit(64)::bigint), 0) + 1 "
    "FROM trace_info WHERE request_id LIKE 'tr-0000000000000000%';"
)

_NEXT_EXPERIMENT_ID_QUERY = "SELECT COALESCE(MAX(experiment_id), 0) + 1 FROM experiments;"

_BUMP_EXPERIMENT_SEQUENCE_SQL = """
SELECT setval(
  pg_get_serial_sequence('experiments', 'experiment_id'),
  (SELECT COALESCE(MAX(experiment_id), 1) FROM experiments)
);
"""

_TUNING_SQL = """
SET synchronous_commit = off;
SET statement_timeout = 0;
SET lock_timeout = 0;
SET work_mem = '256MB';
SET maintenance_work_mem = '1GB';
SET session_replication_role = replica;
"""


def _build_setup_sql(seed: dict) -> str:
    """Drop secondary indexes and (re)populate the span staging table.

    The staging table is a real table, not a TEMP table, because parallel
    workers each open their own psql session/connection and TEMP tables are
    only visible within the session that created them.
    """
    spans_json = json.dumps(seed["spans"], ensure_ascii=False, separators=(",", ":"))
    drop_indexes = "\n".join(f"DROP INDEX IF EXISTS {name};" for name, _, _ in _SECONDARY_INDEXES)
    return f"""
{_TUNING_SQL}

{drop_indexes}

CREATE TABLE IF NOT EXISTS _mlflow_seed_spans (
  span_id text NOT NULL,
  parent_span_id text,
  name text,
  type text,
  status text NOT NULL,
  start_time_unix_nano bigint NOT NULL,
  end_time_unix_nano bigint,
  content text NOT NULL
);

TRUNCATE _mlflow_seed_spans;

INSERT INTO _mlflow_seed_spans (
  span_id, parent_span_id, name, type, status,
  start_time_unix_nano, end_time_unix_nano, content
)
SELECT
  span_id, parent_span_id, name, type, status,
  start_time_unix_nano, end_time_unix_nano, content
FROM json_to_recordset({_dollar("mlflowseed", spans_json)}::json)
AS x(
  span_id text,
  parent_span_id text,
  name text,
  type text,
  status text,
  start_time_unix_nano bigint,
  end_time_unix_nano bigint,
  content text
);
"""


def _build_range_sql(
    seed: dict,
    *,
    workspaces: list[str],
    experiment_prefix: str,
    first_experiment_id: int,
    group_start_id: int,
    traces_per_experiment: int,
    start_id: int,
    end_id: int,
) -> str:
    """Insert shared experiment rows and each trace's data for one id range.

    Group numbering is anchored at `group_start_id` for the entire seed call,
    so calibration, parallel workers, and top-up batches agree on each trace's
    experiment. Concurrent ranges can include the same experiment group;
    conflict-safe experiment inserts make that safe.

    experiment_id is inserted explicitly (bypassing the `experiments` table's
    SERIAL sequence). seed_traces() bumps the sequence past these ids once all
    ranges are done.
    """
    if traces_per_experiment <= 0:
        raise ValueError("traces_per_experiment must be positive")
    info = seed["trace_info"]
    token_usage = json.dumps(seed["token_usage"], ensure_ascii=False, separators=(",", ":"))
    # Stamp clones with "now" rather than the seed file's frozen recording
    # time, so seeded traces fall inside the MLflow UI's default "Last 7
    # Days" filter and any time-windowed dashboards/queries in the test.
    ts_ms = int(time.time() * 1000)
    exec_ms = int(info["execution_time_ms"])
    preview_req = _dollar("p1", info.get("request_preview") or "")
    preview_resp = _dollar("p2", info.get("response_preview") or "")
    token_usage_lit = _dollar("p3", token_usage)
    tname = _dollar("p4", seed.get("trace_name") or seed["source_session_id"])
    workspace_cases = " ".join(
        f"WHEN {index} THEN {_dollar(f'w{index}', workspace)}"
        for index, workspace in enumerate(workspaces)
    )
    group_index_expr = f"((g - {group_start_id}) / {traces_per_experiment})"
    prefix_lit = _dollar("p6", experiment_prefix)
    exp_id_expr = f"({first_experiment_id} + {group_index_expr})"
    workspace_expr = (
        _dollar("p5", workspaces[0])
        if len(workspaces) == 1
        else f"CASE (({exp_id_expr} - 1) % {len(workspaces)}) {workspace_cases} END"
    )

    return f"""
BEGIN;
{_TUNING_SQL}

INSERT INTO experiments (
  experiment_id, name, workspace, artifact_location, lifecycle_stage,
  creation_time, last_update_time
)
SELECT
  seed_experiment_id,
  {prefix_lit} || '-' || seed_experiment_id,
  seed_workspace,
  's3://mlflow-artifacts/' || seed_experiment_id,
  'active',
  {ts_ms}, {ts_ms}
FROM (
  SELECT DISTINCT {exp_id_expr} AS seed_experiment_id,
                  {workspace_expr} AS seed_workspace
  FROM generate_series({start_id}, {end_id}) AS g
) AS seed_experiments
ON CONFLICT (experiment_id) DO NOTHING;

INSERT INTO trace_info (
  request_id, experiment_id, timestamp_ms, execution_time_ms, status,
  client_request_id, request_preview, response_preview, db_payload_generation
)
SELECT
  'tr-' || lpad(to_hex(g), 32, '0'),
  {exp_id_expr}, {ts_ms}, {exec_ms}, 'OK',
  NULL, {preview_req}, {preview_resp}, 0
FROM generate_series({start_id}, {end_id}) AS g;

-- MLflow 3.17.0 has no spans.dimension_attributes column. The complete span,
-- including its attributes, remains serialized in content.
INSERT INTO spans (
  trace_id, experiment_id, span_id, parent_span_id, name, type, status,
  start_time_unix_nano, end_time_unix_nano, content
)
SELECT
  'tr-' || lpad(to_hex(g), 32, '0'),
  {exp_id_expr},
  s.span_id,
  s.parent_span_id,
  s.name,
  s.type,
  s.status,
  s.start_time_unix_nano,
  s.end_time_unix_nano,
  replace(
    replace(
      replace(s.content, '{_TRACE_ID_PH}', 'tr-' || lpad(to_hex(g), 32, '0')),
      '{_TRACE_ID_B64_PH}',
      encode(decode(lpad(to_hex(g), 32, '0'), 'hex'), 'base64')
    ),
    '{_EXPERIMENT_ID_PH}',
    {exp_id_expr}::text
  )
FROM generate_series({start_id}, {end_id}) AS g
CROSS JOIN _mlflow_seed_spans s;

INSERT INTO trace_tags (key, value, request_id)
SELECT 'mlflow.artifactLocation',
       's3://mlflow-artifacts/' || {exp_id_expr} || '/traces/tr-' || lpad(to_hex(g), 32, '0') || '/artifacts',
       'tr-' || lpad(to_hex(g), 32, '0')
FROM generate_series({start_id}, {end_id}) AS g;

INSERT INTO trace_tags (key, value, request_id)
SELECT 'mlflow.traceName', {tname}, 'tr-' || lpad(to_hex(g), 32, '0')
FROM generate_series({start_id}, {end_id}) AS g;

-- MLflow only reads spans from the DB when this tag is TRACKING_STORE;
-- without it get_trace/batch_get_traces skip span loading and serve
-- traces with zero spans.
INSERT INTO trace_tags (key, value, request_id)
SELECT 'mlflow.trace.spansLocation', 'TRACKING_STORE', 'tr-' || lpad(to_hex(g), 32, '0')
FROM generate_series({start_id}, {end_id}) AS g;

INSERT INTO trace_request_metadata (key, value, request_id)
SELECT 'mlflow.trace.tokenUsage', {token_usage_lit}, 'tr-' || lpad(to_hex(g), 32, '0')
FROM generate_series({start_id}, {end_id}) AS g;
COMMIT;
"""


_ANALYZE_SQL = """
ANALYZE trace_info;
ANALYZE spans;
ANALYZE trace_tags;
ANALYZE trace_request_metadata;
ANALYZE experiments;
"""


def _build_finalize_sql() -> str:
    create_indexes = "\n".join(
        f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols};"
        for name, table, cols in _SECONDARY_INDEXES
    )
    return f"""
{create_indexes}

SET session_replication_role = origin;
{_ANALYZE_SQL}
"""


def _dollar(tag: str, value: str) -> str:
    delimiter = f"${tag}$"
    if delimiter in value:
        raise ValueError(f"Seed payload contains SQL delimiter {delimiter}")
    return f"{delimiter}{value}{delimiter}"


# ---------------------------------------------------------------------------
# psql helpers
# ---------------------------------------------------------------------------


def _postgres_pod(namespace: str) -> str:
    result = oc(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        "app=postgresql",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    pod = result.stdout.strip()
    if not pod:
        raise RuntimeError(f"No postgresql pod found in namespace {namespace}")
    return pod


def _exec_sql(pod: str, namespace: str, sql: str, filename: str = "mlflow-seed.sql") -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sql", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(sql)
        local_sql = handle.name

    remote_sql = f"/tmp/{filename}"
    try:
        oc("cp", local_sql, f"{namespace}/{pod}:{remote_sql}")
        oc(
            "exec",
            "-n",
            namespace,
            pod,
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            "mlflow",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            remote_sql,
            log_stdout=False,
            # Seed batches and index rebuilds can legitimately run longer
            # than the generic 300-second oc command timeout. Bulk ranges are
            # bounded transactions and report progress after each batch.
            timeout_seconds=None,
        )
    finally:
        Path(local_sql).unlink(missing_ok=True)


def _scalar(pod: str, namespace: str, sql: str) -> str:
    return oc(
        "exec",
        "-n",
        namespace,
        pod,
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "mlflow",
        "-tA",
        "-c",
        sql,
        log_stdout=False,
    ).stdout.strip()


def _query_lines(pod: str, namespace: str, sql: str) -> list[str]:
    result = oc(
        "exec",
        "-n",
        namespace,
        pod,
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "mlflow",
        "-At",
        "-c",
        sql,
        log_stdout=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _db_size_bytes(pod: str, namespace: str) -> int:
    return int(_scalar(pod, namespace, "SELECT pg_database_size('mlflow');"))


def _pretty_size(pod: str, namespace: str, size_bytes: int) -> str:
    return _scalar(pod, namespace, f"SELECT pg_size_pretty({size_bytes}::bigint);")


if __name__ == "__main__":
    run.main()
