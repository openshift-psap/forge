"""KPI definitions and computation for MCP Gateway Caliper plugin."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from projects.caliper.engine.kpi import (
    Format,
    HigherBetter,
    KpiCatalogEntry,
    KpiComputationStatus,
    KPIMetadata,
    KpiRecord,
    LowerBetter,
    build_catalog_from_functions,
    create_label_extractor,
    get_kpi_functions,
    is_curve_kpi,
)
from projects.caliper.engine.model import UnifiedRunModel


def _require(unified_record, key: str) -> float:
    value = unified_record.metrics.get(key)
    if value is None:
        raise ValueError(f"{key} metric not found")
    return float(value)


# ---------------------------------------------------------------------------
# Locust aggregated (all operations mixed) — kept for continuity
# ---------------------------------------------------------------------------


@HigherBetter()
@Format("{:.5f}")
@KPIMetadata(help="Sustained request throughput", unit="req/s")
def mcp_gw_requests_per_second(unified_record) -> float:
    """Request Rate KPI."""
    return _require(unified_record, "requests_per_second")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Mean response time across all requests", unit="ms")
def mcp_gw_avg_response_time_ms(unified_record) -> float:
    """Average Response Time KPI."""
    return _require(unified_record, "avg_response_time_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Median (P50) response latency", unit="ms")
def mcp_gw_p50_ms(unified_record) -> float:
    """P50 Latency KPI."""
    return _require(unified_record, "p50_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="95th percentile response latency", unit="ms")
def mcp_gw_p95_ms(unified_record) -> float:
    """P95 Latency KPI."""
    return _require(unified_record, "p95_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="99th percentile response latency", unit="ms")
def mcp_gw_p99_ms(unified_record) -> float:
    """P99 Latency KPI."""
    return _require(unified_record, "p99_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Fraction of failed requests", unit="%")
def mcp_gw_failure_rate(unified_record) -> float:
    """Failure Rate KPI."""
    return _require(unified_record, "failure_rate")


# ---------------------------------------------------------------------------
# Locust per-operation (call:* / handshake / tools/list)
# ---------------------------------------------------------------------------


@HigherBetter()
@Format("{:.5f}")
@KPIMetadata(help="Sustained tools/call throughput", unit="req/s")
def mcp_gw_tool_call_rps(unified_record) -> float:
    """Tool Call Rate KPI."""
    return _require(unified_record, "tool_call_rps")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Median (P50) tools/call latency", unit="ms")
def mcp_gw_tool_call_p50_ms(unified_record) -> float:
    """Tool Call P50 Latency KPI."""
    return _require(unified_record, "tool_call_p50_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="95th percentile tools/call latency", unit="ms")
def mcp_gw_tool_call_p95_ms(unified_record) -> float:
    """Tool Call P95 Latency KPI."""
    return _require(unified_record, "tool_call_p95_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="99th percentile tools/call latency", unit="ms")
def mcp_gw_tool_call_p99_ms(unified_record) -> float:
    """Tool Call P99 Latency KPI."""
    return _require(unified_record, "tool_call_p99_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Fraction of failed tools/call requests", unit="%")
def mcp_gw_tool_call_failure_rate(unified_record) -> float:
    """Tool Call Failure Rate KPI."""
    return _require(unified_record, "tool_call_failure_rate")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="95th percentile handshake latency (initialize or server/discover)", unit="ms")
def mcp_gw_handshake_p95_ms(unified_record) -> float:
    """Handshake P95 Latency KPI."""
    return _require(unified_record, "handshake_p95_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(
    help="95th percentile time to first tool response (2026 hot path; no handshake)", unit="ms"
)
def mcp_gw_ttftr_p95_ms(unified_record) -> float:
    """Time to first tool response P95 KPI."""
    return _require(unified_record, "ttftr_p95_ms")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="95th percentile tools/list latency", unit="ms")
def mcp_gw_tools_list_p95_ms(unified_record) -> float:
    """Tools List P95 Latency KPI."""
    return _require(unified_record, "tools_list_p95_ms")


@HigherBetter()
@Format("{:.5f}")
@KPIMetadata(help="tools/list throughput", unit="req/s")
def mcp_gw_tools_list_rps(unified_record) -> float:
    """Tools List Rate KPI."""
    return _require(unified_record, "tools_list_rps")


# ---------------------------------------------------------------------------
# Prometheus: broker (mcp-system) and Envoy gateway (gateway-system)
# ---------------------------------------------------------------------------


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Average broker pod CPU over the test window", unit="cores")
def mcp_gw_broker_cpu_avg_cores(unified_record) -> float:
    """Broker CPU Average KPI."""
    return _require(unified_record, "broker_cpu_avg_cores")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Peak broker pod CPU over the test window", unit="cores")
def mcp_gw_broker_cpu_max_cores(unified_record) -> float:
    """Broker CPU Peak KPI."""
    return _require(unified_record, "broker_cpu_max_cores")


@LowerBetter()
@Format("{:.0f}")
@KPIMetadata(help="Average broker pod memory working set", unit="bytes")
def mcp_gw_broker_memory_avg_bytes(unified_record) -> float:
    """Broker Memory Average KPI."""
    return _require(unified_record, "broker_memory_avg_bytes")


@LowerBetter()
@Format("{:.0f}")
@KPIMetadata(help="Peak broker pod memory working set", unit="bytes")
def mcp_gw_broker_memory_max_bytes(unified_record) -> float:
    """Broker Memory Peak KPI."""
    return _require(unified_record, "broker_memory_max_bytes")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Average Envoy/gateway pod CPU over the test window", unit="cores")
def mcp_gw_envoy_cpu_avg_cores(unified_record) -> float:
    """Envoy CPU Average KPI."""
    return _require(unified_record, "envoy_cpu_avg_cores")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Peak Envoy/gateway pod CPU over the test window", unit="cores")
def mcp_gw_envoy_cpu_max_cores(unified_record) -> float:
    """Envoy CPU Peak KPI."""
    return _require(unified_record, "envoy_cpu_max_cores")


@LowerBetter()
@Format("{:.0f}")
@KPIMetadata(help="Average Envoy/gateway pod memory working set", unit="bytes")
def mcp_gw_envoy_memory_avg_bytes(unified_record) -> float:
    """Envoy Memory Average KPI."""
    return _require(unified_record, "envoy_memory_avg_bytes")


@LowerBetter()
@Format("{:.0f}")
@KPIMetadata(help="Peak Envoy/gateway pod memory working set", unit="bytes")
def mcp_gw_envoy_memory_max_bytes(unified_record) -> float:
    """Envoy Memory Peak KPI."""
    return _require(unified_record, "envoy_memory_max_bytes")


@LowerBetter()
@Format("{:.5f}")
@KPIMetadata(help="Istio HTTP 4xx rate (protocol validation / client errors)", unit="req/s")
def mcp_gw_http_4xx_rate(unified_record) -> float:
    """HTTP 4xx Rate KPI."""
    return _require(unified_record, "http_4xx_rate")


class MCPGatewayKpiHandler:
    """Handles KPI catalog and computation for MCP Gateway benchmarks."""

    LABEL_EXTRACTOR = create_label_extractor(
        {
            "preset": "distinguishing_labels.preset",
            "num_servers": "distinguishing_labels.num_servers",
            "users": "distinguishing_labels.users",
            "target": "distinguishing_labels.target",
            "protocol_mode": "distinguishing_labels.protocol_mode",
            "mcp_gateway_version": "distinguishing_labels.mcp_gateway_version",
            "version_kind": "distinguishing_labels.version_kind",
        }
    )

    @staticmethod
    def get_catalog() -> list[KpiCatalogEntry]:
        """Return the KPI catalog built from decorated functions using dataclasses."""
        current_module = inspect.getmodule(MCPGatewayKpiHandler)
        return build_catalog_from_functions(current_module)

    @staticmethod
    def compute_kpis(model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute KPI values from the unified model."""
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[KpiRecord] = []
        current_module = inspect.getmodule(MCPGatewayKpiHandler)
        kpi_functions = get_kpi_functions(current_module)

        valid_records = [
            r
            for r in model.unified_result_records
            if r.run_identity.get("mcp_gateway") and not r.metrics.get("no_stats_csv_found")
        ]

        if not valid_records:
            status = KpiComputationStatus.success_status(0, len(model.unified_result_records))
            return out, status

        for r in valid_records:
            base_labels = {**r.distinguishing_labels}
            test_condition_labels = MCPGatewayKpiHandler.LABEL_EXTRACTOR.extract(r)

            for kpi_id, kpi_func in kpi_functions.items():
                if is_curve_kpi(kpi_func):
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

                # Create structured KPI record using core dataclass
                kpi_record = KpiRecord(
                    kpi_id=kpi_id,
                    value=value,  # Core enforces int|float only
                    unit=kpi_func._kpi_unit,
                    run_id=r.test_base_path,
                    timestamp=ts,
                    labels=all_labels,
                    is_curve=False,  # Scalar KPI
                )

                out.append(kpi_record)

        # Create success status
        status = KpiComputationStatus.success_status(len(valid_records), len(valid_records))
        return out, status
