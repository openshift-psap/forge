"""Parse AI Perf summary exports into Caliper records and tool-specific KPIs."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from projects.caliper.engine.kpi import KpiComputationStatus, KpiRecord
from projects.caliper.engine.model import (
    BaseTestNode,
    ParseResult,
    UnifiedResultRecord,
    UnifiedRunModel,
)

_METRICS = {
    "request_count": ("Request count", "requests"),
    "error_request_count": ("Error request count", "requests"),
    "request_throughput": ("Request throughput", "requests/sec"),
    "output_token_throughput": ("Output token throughput", "tokens/sec"),
    "time_to_first_token": ("Time to first token", "ms"),
    "inter_token_latency": ("Inter token latency", "ms"),
    "request_latency": ("Request latency", "ms"),
}


def benchmark_tool(node: BaseTestNode) -> str:
    labels = node.test_labels.get("labels", node.test_labels)
    if not isinstance(labels, dict):
        raise ValueError(f"Invalid benchmark labels for {node.test_path}")
    tool = labels.get("benchmark_tool")
    if tool not in ("guidellm", "aiperf"):
        raise ValueError(f"Missing or unsupported benchmark_tool for {node.test_path}: {tool!r}")
    return tool


def model_for_tool(model: UnifiedRunModel, tool: str) -> UnifiedRunModel:
    """Restrict an existing Caliper model to one benchmark tool."""
    return replace(
        model,
        test_nodes=[node for node in model.test_nodes if benchmark_tool(node) == tool],
        unified_result_records=[
            record for record in model.unified_result_records if record.run_identity.get(tool)
        ],
    )


class AIPerfParser:
    def parse(self, nodes: list[BaseTestNode]) -> ParseResult:
        records = []
        for node in nodes:
            summaries = [
                path for path in node.artifact_paths if path.name == "profile_export_aiperf.json"
            ]
            if len(summaries) != 1:
                raise ValueError(
                    f"Expected one AI Perf summary for {node.test_path}, found {len(summaries)}"
                )
            try:
                summary = json.loads(summaries[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid AI Perf summary for {node.test_path}") from exc
            if not isinstance(summary, dict) or "request_count" not in summary:
                raise ValueError(f"AI Perf summary lacks request_count for {node.test_path}")
            labels = node.test_labels.get("labels", node.test_labels)
            records.append(
                UnifiedResultRecord(
                    test_base_path=str(node.test_path),
                    distinguishing_labels=dict(labels),
                    metrics={"aiperf_summary": summary},
                    run_identity={"aiperf": True},
                )
            )
        return ParseResult(records=records)


def _metric_value(summary: dict[str, Any], key: str, field: str, unit: str) -> float | None:
    metric = summary.get(key)
    if metric is None:
        return None
    if not isinstance(metric, dict) or metric.get("unit") != unit:
        raise ValueError(f"AI Perf metric {key} has an unexpected unit")
    value = metric.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"AI Perf metric {key}.{field} is not numeric") from exc


def compute_aiperf_kpis(model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
    """Keep AI Perf KPI names distinct from GuideLLM KPI names."""
    rows: list[KpiRecord] = []
    timestamp = datetime.now(UTC).isoformat()
    records = [
        record for record in model.unified_result_records if record.run_identity.get("aiperf")
    ]
    for record in records:
        summary = record.metrics["aiperf_summary"]
        for key, (name, unit) in _METRICS.items():
            fields = (
                ("avg", "p95")
                if key in ("time_to_first_token", "inter_token_latency", "request_latency")
                else ("avg",)
            )
            for field in fields:
                value = _metric_value(summary, key, field, unit)
                if value is None:
                    continue
                rows.append(
                    KpiRecord(
                        kpi_id=f"aiperf_{key}_{field}",
                        name=f"AI Perf {name} {field}",
                        value=value,
                        unit=unit,
                        higher_is_better=key
                        in ("request_throughput", "output_token_throughput", "request_count"),
                        labels={
                            key: str(value) for key, value in record.distinguishing_labels.items()
                        },
                        metadata={"benchmark_tool": "aiperf"},
                        run_id=record.test_base_path,
                        timestamp=timestamp,
                    )
                )
    return rows, KpiComputationStatus.success_status(len(records))
