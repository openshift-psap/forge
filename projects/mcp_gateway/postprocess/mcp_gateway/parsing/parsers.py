"""MCP Gateway Caliper parser: reads Locust stats.csv artifacts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from projects.agentic_tools.locust.helpers.parse_results import RunMetrics, parse_stats_csv
from projects.caliper.engine.model import (
    ParseResult,
    TestBaseNode,
    UnifiedResultRecord,
)

logger = logging.getLogger(__name__)

STATS_CSV = "stats.csv"
METRICS_FILE = "metrics.json"
PARAMETERS_FILE = "parameters.json"


def _labels_from_node(node: TestBaseNode) -> dict[str, Any]:
    """Extract distinguishing labels from a test node."""
    raw = node.labels
    inner = raw.get("labels")
    if isinstance(inner, dict):
        return dict(inner)
    if isinstance(raw, dict):
        return dict(raw)
    return {"facet": "default"}


def _run_metrics_to_dict(metrics: RunMetrics) -> dict[str, Any]:
    """Convert RunMetrics to a flat dictionary suitable for metrics.json."""
    return {
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


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


class MCPGatewayParser:
    """Parser for Locust stats.csv artifacts from MCP Gateway tests."""

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
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

                _write_json(node.directory / METRICS_FILE, metrics_dict)

                params = {str(k): ("" if v is None else str(v)) for k, v in labels.items()}
                _write_json(node.directory / PARAMETERS_FILE, params)

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
