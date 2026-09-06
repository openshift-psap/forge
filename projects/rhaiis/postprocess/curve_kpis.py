"""Curve KPI definitions for RHAIIS dashboard metrics."""

from __future__ import annotations

from projects.caliper.engine.kpi import (
    Curve,
    HigherBetter,
    KPIMetadata,
    LowerBetter,
)


@HigherBetter()
@Curve(
    x_unit="connections",
    x_help="Input concurrency",
    y_unit="tokens/s",
    y_help="Output token throughput",
    x_format="{:.0f}",
    y_format="{:.1f}",
)
@KPIMetadata(help="Output token throughput at different concurrency levels", unit="tokens/s")
def rhaiis_output_tok_per_sec_curve(unified_record) -> list[tuple[int, float]]:
    """Output Token Throughput vs Concurrency Curve KPI."""
    curves = unified_record.metrics.get("performance_curves", {})
    intended_concurrency = curves.get("intended_concurrency", [])
    output_tok_per_sec = curves.get("output_tok_per_sec", [])

    if (
        not intended_concurrency
        or not output_tok_per_sec
        or len(intended_concurrency) != len(output_tok_per_sec)
    ):
        return []

    return [
        (int(x), float(y))
        for x, y in zip(intended_concurrency, output_tok_per_sec, strict=False)
        if x > 0 and y > 0
    ]


@HigherBetter()
@Curve(
    x_unit="connections",
    x_help="Input concurrency",
    y_unit="tokens/s",
    y_help="Total token throughput",
    x_format="{:.0f}",
    y_format="{:.1f}",
)
@KPIMetadata(help="Total token throughput at different concurrency levels", unit="tokens/s")
def rhaiis_total_tok_per_sec_curve(unified_record) -> list[tuple[int, float]]:
    """Total Token Throughput vs Concurrency Curve KPI."""
    curves = unified_record.metrics.get("performance_curves", {})
    intended_concurrency = curves.get("intended_concurrency", [])
    total_tok_per_sec = curves.get("total_tok_per_sec", [])

    if (
        not intended_concurrency
        or not total_tok_per_sec
        or len(intended_concurrency) != len(total_tok_per_sec)
    ):
        return []

    return [
        (int(x), float(y))
        for x, y in zip(intended_concurrency, total_tok_per_sec, strict=False)
        if x > 0 and y > 0
    ]


@LowerBetter()
@Curve(
    x_unit="connections",
    x_help="Input concurrency",
    y_unit="s",
    y_help="Time to first token P95",
    x_format="{:.0f}",
    y_format="{:.4f}",
)
@KPIMetadata(help="TTFT P95 latency at different concurrency levels", unit="s")
def rhaiis_ttft_p95_curve(unified_record) -> list[tuple[int, float]]:
    """TTFT P95 Latency vs Concurrency Curve KPI."""
    curves = unified_record.metrics.get("performance_curves", {})
    intended_concurrency = curves.get("intended_concurrency", [])
    ttft_p95 = curves.get("ttft_p95", [])

    if not intended_concurrency or not ttft_p95 or len(intended_concurrency) != len(ttft_p95):
        return []

    return [
        (int(x), float(y))
        for x, y in zip(intended_concurrency, ttft_p95, strict=False)
        if x > 0 and y > 0
    ]


@HigherBetter()
@Curve(
    x_unit="connections",
    x_help="Input concurrency",
    y_unit="req/s",
    y_help="Measured request rate",
    x_format="{:.0f}",
    y_format="{:.1f}",
)
@KPIMetadata(help="Measured request rate at different concurrency levels", unit="req/s")
def rhaiis_measured_rps_curve(unified_record) -> list[tuple[int, float]]:
    """Measured RPS vs Concurrency Curve KPI."""
    curves = unified_record.metrics.get("performance_curves", {})
    intended_concurrency = curves.get("intended_concurrency", [])
    measured_rps = curves.get("measured_rps", [])

    if (
        not intended_concurrency
        or not measured_rps
        or len(intended_concurrency) != len(measured_rps)
    ):
        return []

    return [
        (int(x), float(y))
        for x, y in zip(intended_concurrency, measured_rps, strict=False)
        if x > 0 and y > 0
    ]
