"""PostgreSQL performance stats capture for post-test analysis.

Captures pg_stat_statements, connection state, per-table stats,
cache hit ratios, and table sizes from the test PostgreSQL instance.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_QUERIES: dict[str, str] = {
    "pg_stat_statements.txt": (
        "SELECT calls,"
        " round(total_exec_time::numeric, 2) AS total_ms,"
        " round((total_exec_time / calls)::numeric, 2) AS avg_ms,"
        " round(max_exec_time::numeric, 2) AS max_ms,"
        " round(min_exec_time::numeric, 2) AS min_ms,"
        " round(stddev_exec_time::numeric, 2) AS stddev_ms,"
        " left(query, 200) AS query_preview"
        " FROM pg_stat_statements"
        " WHERE userid = (SELECT usesysid FROM pg_user WHERE usename = 'mlflow')"
        " ORDER BY total_exec_time DESC"
        " LIMIT 30;"
    ),
    "pg_stat_activity.txt": (
        "SELECT state, wait_event_type, wait_event, count(*) AS count"
        " FROM pg_stat_activity"
        " WHERE datname = 'mlflow'"
        " GROUP BY state, wait_event_type, wait_event"
        " ORDER BY count DESC;"
    ),
    "pg_stat_user_tables.txt": (
        "SELECT relname AS table,"
        " seq_scan, idx_scan,"
        " n_tup_ins AS inserts, n_tup_upd AS updates, n_tup_del AS deletes,"
        " n_live_tup AS live_rows, n_dead_tup AS dead_rows"
        " FROM pg_stat_user_tables"
        " ORDER BY n_tup_ins DESC;"
    ),
    "pg_database_stats.txt": (
        "SELECT"
        " xact_commit AS commits, xact_rollback AS rollbacks,"
        " round(100.0 * xact_rollback / NULLIF(xact_commit + xact_rollback, 0), 2)"
        "   AS rollback_pct,"
        " blks_hit, blks_read,"
        " round(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2)"
        "   AS cache_hit_pct,"
        " temp_files, temp_bytes,"
        " deadlocks,"
        " pg_size_pretty(pg_database_size('mlflow')) AS db_size"
        " FROM pg_stat_database"
        " WHERE datname = 'mlflow';"
    ),
    "pg_table_sizes.txt": (
        "SELECT relname AS table,"
        " pg_size_pretty(pg_total_relation_size(relid)) AS total_size,"
        " pg_size_pretty(pg_relation_size(relid)) AS table_size,"
        " pg_size_pretty(pg_indexes_size(relid)) AS index_size"
        " FROM pg_stat_user_tables"
        " ORDER BY pg_total_relation_size(relid) DESC;"
    ),
    "pg_block_io_time.txt": (
        # avg_write_ms is intentionally omitted: pg_stat_database exposes no
        # "blocks written" counter, so any average built from blk_write_time
        # would have to divide by an unrelated (read) count. blk_write_time
        # itself is cumulative since the last database-wide stats reset
        # and requires track_io_timing=on. Expect it to stay at/near 0 when the
        # working set is fully cache-resident or the kernel clock source
        # used by the container has too little resolution for these calls.
        "SELECT"
        " blk_read_time AS read_time_ms,"
        " blk_write_time AS write_time_ms,"
        " blks_read, blks_hit,"
        " round((blk_read_time / NULLIF(blks_read, 0))::numeric, 4) AS avg_read_ms"
        " FROM pg_stat_database"
        " WHERE datname = 'mlflow';"
    ),
    "pg_statio_user_tables.txt": (
        "SELECT relname AS table,"
        " heap_blks_read, heap_blks_hit,"
        " round(100.0 * heap_blks_hit / NULLIF(heap_blks_hit + heap_blks_read, 0), 2)"
        "   AS heap_hit_pct,"
        " idx_blks_read, idx_blks_hit,"
        " round(100.0 * idx_blks_hit / NULLIF(idx_blks_hit + idx_blks_read, 0), 2)"
        "   AS idx_hit_pct,"
        " toast_blks_read, toast_blks_hit,"
        " tidx_blks_read, tidx_blks_hit"
        " FROM pg_statio_user_tables"
        " ORDER BY heap_blks_read + idx_blks_read DESC;"
    ),
    "pg_stat_bgwriter.txt": (
        "SELECT buffers_clean, maxwritten_clean, buffers_alloc, stats_reset FROM pg_stat_bgwriter;"
    ),
    "pg_stat_checkpointer.txt": (
        "SELECT num_timed AS scheduled_checkpoints,"
        " num_requested AS requested_checkpoints,"
        " num_done AS completed_checkpoints,"
        " round(write_time::numeric, 2) AS checkpoint_write_ms,"
        " round(sync_time::numeric, 2) AS checkpoint_sync_ms,"
        " buffers_written, stats_reset"
        " FROM pg_stat_checkpointer;"
    ),
    "pg_stat_wal.txt": (
        # PostgreSQL 18 exposes WAL generation and WAL I/O in separate views.
        "SELECT wal_records, wal_fpi,"
        " pg_size_pretty(wal_bytes) AS wal_bytes,"
        " wal_buffers_full, io.wal_write, io.wal_sync,"
        " io.wal_write_ms, io.wal_sync_ms"
        " FROM pg_stat_wal"
        " CROSS JOIN ("
        " SELECT coalesce(sum(writes), 0) AS wal_write,"
        " coalesce(sum(fsyncs), 0) AS wal_sync,"
        " round(coalesce(sum(write_time), 0)::numeric, 2) AS wal_write_ms,"
        " round(coalesce(sum(fsync_time), 0)::numeric, 2) AS wal_sync_ms"
        " FROM pg_stat_io WHERE object = 'wal'"
        " ) AS io;"
    ),
    "pg_lock_waits.txt": (
        "SELECT"
        " blocked.pid AS blocked_pid,"
        " blocked.query AS blocked_query,"
        " blocking.pid AS blocking_pid,"
        " blocking.query AS blocking_query,"
        " blocked.wait_event_type, blocked.wait_event,"
        " blocked.state AS blocked_state"
        " FROM pg_stat_activity AS blocked"
        " JOIN pg_locks AS bl ON bl.pid = blocked.pid AND NOT bl.granted"
        " JOIN pg_locks AS kl ON kl.transactionid = bl.transactionid AND kl.granted"
        " JOIN pg_stat_activity AS blocking ON blocking.pid = kl.pid"
        " WHERE blocked.datname = 'mlflow'"
        " LIMIT 50;"
    ),
    "pg_lock_summary.txt": (
        "SELECT"
        " wait_event_type, wait_event, state,"
        " count(*) AS waiting_count"
        " FROM pg_stat_activity"
        " WHERE datname = 'mlflow'"
        " AND wait_event_type = 'Lock'"
        " GROUP BY wait_event_type, wait_event, state"
        " ORDER BY waiting_count DESC;"
    ),
}


def reset_pg_stat_statements(
    *,
    namespace: str,
    database: str = "mlflow",
) -> bool:
    """Reset the query, background writer, checkpoint, WAL, and I/O counters before measurement."""
    from projects.core.dsl.utils.k8s import oc

    ok = True
    for label, stmt in (
        ("pg_stat_statements", "SELECT pg_stat_statements_reset();"),
        ("bgwriter", "SELECT pg_stat_reset_shared('bgwriter');"),
        ("checkpointer", "SELECT pg_stat_reset_shared('checkpointer');"),
        ("wal", "SELECT pg_stat_reset_shared('wal');"),
        ("io", "SELECT pg_stat_reset_shared('io');"),
    ):
        result = oc(
            "exec",
            "-n",
            namespace,
            "deployment/postgresql",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-c",
            stmt,
            check=False,
            log_stdout=False,
        )
        if result.returncode != 0:
            logger.warning("Failed to reset %s counters: %s", label, result.stderr)
            ok = False
        else:
            logger.info("%s counters reset", label)
    return ok


def capture(
    *,
    namespace: str,
    output_dir: Path,
    database: str = "mlflow",
    subdirectory: str = "pg_stats",
) -> None:
    """Capture all PostgreSQL performance stats to output_dir/<subdirectory>/."""
    from projects.core.dsl.utils.k8s import oc

    pg_dir = output_dir / subdirectory
    pg_dir.mkdir(parents=True, exist_ok=True)

    for filename, query in _QUERIES.items():
        logger.info("Capturing %s", filename)
        dest = pg_dir / filename
        result = oc(
            "exec",
            "-n",
            namespace,
            "deployment/postgresql",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-c",
            query,
            check=False,
            log_stdout=False,
            stdout_dest=dest,
        )
        if result.returncode != 0:
            logger.warning("Failed to capture %s: %s", filename, result.stderr)
