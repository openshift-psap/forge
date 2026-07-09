"""KPI definitions and computation for MCP Gateway Caliper plugin."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from projects.caliper.engine.kpi import (
    Format,
    HigherBetter,
    KPIMetadata,
    LowerBetter,
    build_catalog_from_functions,
    create_label_extractor,
    get_kpi_functions,
    is_2d_kpi,
)
from projects.caliper.engine.model import UnifiedRunModel


@HigherBetter()
@Format("{:.5f}")
@KPIMetadata(help="Sustained request throughput", unit="req/s")
def mcp_gw_requests_per_second(unified_record) -> float:
    """Request Rate KPI."""
    value = unified_record.metrics.get("requests_per_second")
    if value is None:
        raise ValueError("requests_per_second metric not found")
    return float(value)


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Mean response time across all requests", unit="ms")
def mcp_gw_avg_response_time_ms(unified_record) -> float:
    """Average Response Time KPI."""
    value = unified_record.metrics.get("avg_response_time_ms")
    if value is None:
        raise ValueError("avg_response_time_ms metric not found")
    return float(value)


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Median (P50) response latency", unit="ms")
def mcp_gw_p50_ms(unified_record) -> float:
    """P50 Latency KPI."""
    value = unified_record.metrics.get("p50_ms")
    if value is None:
        raise ValueError("p50_ms metric not found")
    return float(value)


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="95th percentile response latency", unit="ms")
def mcp_gw_p95_ms(unified_record) -> float:
    """P95 Latency KPI."""
    value = unified_record.metrics.get("p95_ms")
    if value is None:
        raise ValueError("p95_ms metric not found")
    return float(value)


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="99th percentile response latency", unit="ms")
def mcp_gw_p99_ms(unified_record) -> float:
    """P99 Latency KPI."""
    value = unified_record.metrics.get("p99_ms")
    if value is None:
        raise ValueError("p99_ms metric not found")
    return float(value)


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Fraction of failed requests", unit="%")
def mcp_gw_failure_rate(unified_record) -> float:
    """Failure Rate KPI."""
    value = unified_record.metrics.get("failure_rate")
    if value is None:
        raise ValueError("failure_rate metric not found")
    return float(value)


class MCPGatewayKpiHandler:
    """Handles KPI catalog and computation for MCP Gateway benchmarks."""

    LABEL_EXTRACTOR = create_label_extractor(
        {
            "preset": "distinguishing_labels.preset",
            "num_servers": "distinguishing_labels.num_servers",
            "users": "distinguishing_labels.users",
        }
    )

    @staticmethod
    def get_catalog() -> list[dict[str, Any]]:
        """Return the KPI catalog built from decorated functions."""
        current_module = inspect.getmodule(MCPGatewayKpiHandler)
        return build_catalog_from_functions(current_module)

    @staticmethod
    def compute_kpis(model: UnifiedRunModel) -> list[dict[str, Any]]:
        """Compute KPI values from the unified model."""
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict[str, Any]] = []
        current_module = inspect.getmodule(MCPGatewayKpiHandler)
        kpi_functions = get_kpi_functions(current_module)

        valid_records = [
            r
            for r in model.unified_result_records
            if r.run_identity.get("mcp_gateway") and not r.metrics.get("no_stats_csv_found")
        ]

        if not valid_records:
            return out

        for r in valid_records:
            base_labels = {**r.distinguishing_labels}
            test_condition_labels = MCPGatewayKpiHandler.LABEL_EXTRACTOR.extract(r)

            for kpi_id, kpi_func in kpi_functions.items():
                if is_2d_kpi(kpi_func):
                    continue

                try:
                    value = kpi_func(r)
                except (TypeError, ValueError, KeyError):
                    value = None

                if value is None:
                    continue

                all_labels = {
                    **base_labels,
                    **test_condition_labels,
                    "higher_is_better": kpi_func._kpi_higher_is_better,
                }

                out.append(
                    {
                        "schema_version": "1",
                        "kpi_id": kpi_id,
                        "value": value,
                        "unit": kpi_func._kpi_unit,
                        "run_id": r.test_base_path,
                        "timestamp": ts,
                        "labels": all_labels,
                        "source": {
                            "test_base_path": r.test_base_path,
                            "plugin_module": model.plugin_module,
                        },
                    }
                )

        return out
