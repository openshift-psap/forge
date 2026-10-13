"""MCP Gateway Caliper parser: Locust stats.csv plus Prometheus capture JSON."""

from __future__ import annotations

import logging
from typing import Any

from projects.agentic_tools.locust.helpers.parse_results import RunMetrics, parse_stats_csv
from projects.caliper.engine.model import (
    BaseTestNode,
    ParseResult,
    UnifiedResultRecord,
)

from .prom_summary import summarize_prom_artifacts

logger = logging.getLogger(__name__)

STATS_CSV = "stats.csv"

_HANDSHAKE_NAMES = ("initialize", "server/discover", "discover")
_TOOLS_LIST_NAME = "tools/list"
_TTFTR_NAME = "ttftr"


def _labels_from_node(node: BaseTestNode) -> dict[str, Any]:
    """Extract distinguishing labels from a test node."""
    raw = node.test_labels
    inner = raw.get("labels")
    if isinstance(inner, dict):
        return dict(inner)
    if isinstance(raw, dict):
        return dict(raw)
    return {"facet": "default"}


def _run_metrics_to_dict(metrics: RunMetrics) -> dict[str, Any]:
    """Convert RunMetrics to a flat dictionary for unified result records."""
    out: dict[str, Any] = {
        "total_requests": metrics.total_requests,
        "total_failures": metrics.total_failures,
        "failure_rate": round(metrics.failure_rate, 6),
        "avg_response_time_ms": round(metrics.avg_response_time_ms, 3),
        "p50_ms": round(metrics.p50_ms, 3),
        "p90_ms": round(metrics.p90_ms, 3),
        "p95_ms": round(metrics.p95_ms, 3),
        "p99_ms": round(metrics.p99_ms, 3),
        "max_ms": round(metrics.max_ms, 3),
        "requests_per_second": round(metrics.requests_per_second, 3),
    }
    out.update(_operation_metrics(metrics.per_request_metrics))
    return out


def _row_name(key: str) -> str:
    """Strip the Locust Type prefix (``MCP:call:alpha`` → ``call:alpha``)."""
    if ":" not in key:
        return key
    return key.split(":", 1)[1]


def _operation_metrics(per_request: dict[str, dict[str, float]]) -> dict[str, float]:
    """Flatten Locust per-name rows into handshake / tools/list / tool-call scalars."""
    out: dict[str, float] = {}

    call_ok: list[dict[str, float]] = []
    call_fail: list[dict[str, float]] = []
    for key, row in per_request.items():
        name = _row_name(key)
        if name.startswith("FAIL:call:"):
            call_fail.append(row)
        elif name.startswith("call:"):
            call_ok.append(row)

    if call_ok or call_fail:
        ok_count = sum(row.get("count", 0) for row in call_ok)
        fail_from_fail_rows = sum(row.get("count", 0) for row in call_fail)
        fail_from_ok_rows = sum(row.get("failures", 0) for row in call_ok)
        fail_count = fail_from_fail_rows + fail_from_ok_rows
        total = ok_count + fail_from_fail_rows
        out["tool_call_rps"] = round(sum(row.get("rps", 0.0) for row in call_ok + call_fail), 3)
        if total > 0:
            out["tool_call_failure_rate"] = round(fail_count / total, 6)
        if call_ok:
            for percentile in ("p50_ms", "p95_ms", "p99_ms"):
                weighted = _weighted_avg(call_ok, percentile)
                if weighted is not None:
                    out[f"tool_call_{percentile}"] = round(weighted, 3)

    handshake = _first_named_row(per_request, _HANDSHAKE_NAMES)
    if handshake is not None:
        out["handshake_p95_ms"] = round(handshake.get("p95_ms", 0.0), 3)

    ttftr = _first_named_row(per_request, (_TTFTR_NAME,))
    if ttftr is not None:
        out["ttftr_p95_ms"] = round(ttftr.get("p95_ms", 0.0), 3)

    tools_list = _first_named_row(per_request, (_TOOLS_LIST_NAME,))
    if tools_list is not None:
        out["tools_list_p95_ms"] = round(tools_list.get("p95_ms", 0.0), 3)
        out["tools_list_rps"] = round(tools_list.get("rps", 0.0), 3)

    return out


def _first_named_row(
    per_request: dict[str, dict[str, float]],
    names: tuple[str, ...],
) -> dict[str, float] | None:
    by_name = {_row_name(key): row for key, row in per_request.items()}
    for name in names:
        row = by_name.get(name)
        if row is not None:
            return row
    return None


def _weighted_avg(rows: list[dict[str, float]], field: str) -> float | None:
    total = sum(row.get("count", 0) for row in rows)
    if total <= 0:
        return None
    return sum(row.get(field, 0.0) * row.get("count", 0) for row in rows) / total


class MCPGatewayParser:
    """Parser for Locust stats.csv artifacts from MCP Gateway tests."""

    def parse(self, nodes: list[BaseTestNode]) -> ParseResult:
        records: list[UnifiedResultRecord] = []
        warnings: list[str] = []

        for node in nodes:
            stats_files = [p for p in node.artifact_paths if p.name == STATS_CSV]

            if not stats_files:
                labels = _labels_from_node(node)
                records.append(
                    UnifiedResultRecord(
                        test_base_path=str(node.test_path),
                        distinguishing_labels=labels,
                        metrics={"no_stats_csv_found": True},
                        run_identity={"mcp_gateway": True},
                        parse_notes=["No stats.csv file found"],
                    )
                )
                continue

            for stats_file in stats_files:
                try:
                    csv_text = stats_file.read_text(encoding="utf-8")
                    run_metrics = parse_stats_csv(csv_text)
                except Exception as e:
                    warnings.append(f"Failed to parse {stats_file}: {e}")
                    continue

                labels = _labels_from_node(node)
                metrics_dict = _run_metrics_to_dict(run_metrics)
                metrics_dict.update(summarize_prom_artifacts(node.artifact_paths))

                records.append(
                    UnifiedResultRecord(
                        test_base_path=str(node.test_path),
                        distinguishing_labels=labels,
                        metrics=metrics_dict,
                        run_identity={"mcp_gateway": True},
                        parse_notes=[],
                    )
                )

        logger.info("MCP Gateway parser created %d unified result records", len(records))
        return ParseResult(records=records, warnings=warnings)
