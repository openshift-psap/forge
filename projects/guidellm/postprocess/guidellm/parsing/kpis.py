"""KPI definitions and computation for GuideLLM Caliper plugin."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from projects.caliper.engine.kpi import (
    Format,
    HigherBetter,
    KPIMetadata,
    LowerBetter,
    TwoDimensional,
    build_catalog_from_functions,
    create_label_extractor,
    get_kpi_functions,
    is_2d_kpi,
)
from projects.caliper.engine.model import UnifiedRunModel


@HigherBetter()
@Format("{:.1f}")
@KPIMetadata(help="Request concurrency level", unit="connections")
def guidellm_request_concurrency(unified_record) -> float:
    """Request Concurrency KPI."""
    value = unified_record.metrics.get("request_concurrency")
    if value is None:
        raise ValueError("request_concurrency metric not found")
    return float(value)


# Token Count Statistics KPIs - static values
@LowerBetter()
@Format("{:.1f}")
@KPIMetadata(help="Average input tokens per request", unit="tokens")
def guidellm_input_tokens_per_request(unified_record) -> float:
    """Input Tokens Per Request KPI."""
    value = unified_record.metrics.get("input_tokens_per_request")
    if value is None:
        raise ValueError("input_tokens_per_request metric not found")
    return float(value)


@LowerBetter()
@Format("{:.1f}")
@KPIMetadata(help="Average output tokens per request", unit="tokens")
def guidellm_output_tokens_per_request(unified_record) -> float:
    """Output Tokens Per Request KPI."""
    value = unified_record.metrics.get("output_tokens_per_request")
    if value is None:
        raise ValueError("output_tokens_per_request metric not found")
    return float(value)


# Note: Scalar "best" KPIs removed - only 2D curve KPIs are desired


# Note: Total request count KPIs removed - only 2D curve KPIs are desired


# 2D KPIs that extract data from performance curves
@HigherBetter()
@TwoDimensional(
    x_unit="req/s",
    x_help="Request rate",
    y_unit="tokens/s",
    y_help="Achieved throughput",
    x_format="{:.1f}",
    y_format="{:.1f}",
)
@KPIMetadata(help="Throughput achieved at different request rates", unit="tokens/s")
def guidellm_throughput_curve(unified_record) -> list[tuple[float, float]]:
    """Throughput vs Request Rate Curve KPI."""
    request_rates = unified_record.metrics.get("request_rate", [])
    curves = unified_record.metrics.get("performance_curves", {})
    tokens_per_sec = curves.get("tokens_per_second", [])

    if not request_rates or not tokens_per_sec or len(request_rates) != len(tokens_per_sec):
        return []

    return [
        (float(x), float(y))
        for x, y in zip(request_rates, tokens_per_sec, strict=False)
        if x > 0 and y > 0
    ]


@LowerBetter()
@TwoDimensional(
    x_unit="req/s",
    x_help="Request rate",
    y_unit="s",
    y_help="P95 latency",
    x_format="{:.1f}",
    y_format="{:.4f}",
)
@KPIMetadata(help="P95 latency at different request rates", unit="s")
def guidellm_latency(unified_record) -> list[tuple[float, float]]:
    """P95 Latency vs Load Curve KPI."""
    request_rates = unified_record.metrics.get("request_rate", [])
    curves = unified_record.metrics.get("performance_curves", {})
    p95_latency = curves.get("request_latency_p95", [])

    if not request_rates or not p95_latency or len(request_rates) != len(p95_latency):
        return []

    return [
        (float(x), float(y))
        for x, y in zip(request_rates, p95_latency, strict=False)
        if x > 0 and y > 0
    ]


@LowerBetter()
@TwoDimensional(
    x_unit="req/s",
    x_help="Request rate",
    y_unit="s",
    y_help="TTFT P95",
    x_format="{:.1f}",
    y_format="{:.4f}",
)
@KPIMetadata(help="Time to first token P95 at different request rates", unit="s")
def guidellm_ttft(unified_record) -> list[tuple[float, float]]:
    """TTFT P95 vs Load Curve KPI."""
    request_rates = unified_record.metrics.get("request_rate", [])
    curves = unified_record.metrics.get("performance_curves", {})
    ttft_p95 = curves.get("ttft_p95", [])

    if not request_rates or not ttft_p95 or len(request_rates) != len(ttft_p95):
        return []

    return [
        (float(x), float(y))
        for x, y in zip(request_rates, ttft_p95, strict=False)
        if x > 0 and y > 0
    ]


@LowerBetter()
@TwoDimensional(
    x_unit="req/s",
    x_help="Request rate",
    y_unit="s",
    y_help="TPOT median",
    x_format="{:.1f}",
    y_format="{:.4f}",
)
@KPIMetadata(help="Time per output token median at different request rates", unit="s")
def guidellm_tpot(unified_record) -> list[tuple[float, float]]:
    """TPOT Median vs Load Curve KPI."""
    request_rates = unified_record.metrics.get("request_rate", [])
    curves = unified_record.metrics.get("performance_curves", {})
    tpot_median = curves.get("tpot_median", [])

    if not request_rates or not tpot_median or len(request_rates) != len(tpot_median):
        return []

    return [
        (float(x), float(y))
        for x, y in zip(request_rates, tpot_median, strict=False)
        if x > 0 and y > 0
    ]


@LowerBetter()
@TwoDimensional(
    x_unit="req/s",
    x_help="Request rate",
    y_unit="s",
    y_help="TPOT P95",
    x_format="{:.1f}",
    y_format="{:.4f}",
)
@KPIMetadata(help="Time per output token P95 at different request rates", unit="s")
def guidellm_tpot_p95(unified_record) -> list[tuple[float, float]]:
    """TPOT P95 vs Load Curve KPI."""
    request_rates = unified_record.metrics.get("request_rate", [])
    curves = unified_record.metrics.get("performance_curves", {})
    tpot_p95 = curves.get("tpot_p95", [])

    if not request_rates or not tpot_p95 or len(request_rates) != len(tpot_p95):
        return []

    return [
        (float(x), float(y))
        for x, y in zip(request_rates, tpot_p95, strict=False)
        if x > 0 and y > 0
    ]


@LowerBetter()
@TwoDimensional(
    x_unit="req/s",
    x_help="Request rate",
    y_unit="s",
    y_help="TPOT P99",
    x_format="{:.1f}",
    y_format="{:.4f}",
)
@KPIMetadata(help="Time per output token P99 at different request rates", unit="s")
def guidellm_tpot_p99(unified_record) -> list[tuple[float, float]]:
    """TPOT P99 vs Load Curve KPI."""
    request_rates = unified_record.metrics.get("request_rate", [])
    curves = unified_record.metrics.get("performance_curves", {})
    tpot_p99 = curves.get("tpot_p99", [])

    if not request_rates or not tpot_p99 or len(request_rates) != len(tpot_p99):
        return []

    return [
        (float(x), float(y))
        for x, y in zip(request_rates, tpot_p99, strict=False)
        if x > 0 and y > 0
    ]


class GuideLLMKpiHandler:
    """Handles KPI catalog and computation for GuideLLM benchmarks."""

    # Define label extractor for all GuideLLM test conditions
    LABEL_EXTRACTOR = create_label_extractor(
        {
            "strategy": "metrics.strategy",
            "duration": "metrics.duration",
        }
    )

    # Metadata fields to include in KPI records but not as labels
    @staticmethod
    def extract_metadata(record) -> dict[str, Any]:
        """Extract metadata fields for KPI records."""
        config = record.metrics.get("configuration", {})
        return {
            "configuration": config,
            "run_path": record.test_base_path,
        }

    @staticmethod
    def get_catalog() -> list[dict[str, Any]]:
        """
        Return the KPI catalog for GuideLLM metrics.

        Returns:
            List of KPI definitions
        """
        current_module = inspect.getmodule(GuideLLMKpiHandler)
        return build_catalog_from_functions(current_module)

    @staticmethod
    def compute_kpis(model: UnifiedRunModel) -> list[dict[str, Any]]:
        """
        Compute KPI values from the unified model.

        Args:
            model: Unified model containing parsed test results

        Returns:
            List of KPI records
        """
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict[str, Any]] = []
        current_module = inspect.getmodule(GuideLLMKpiHandler)
        kpi_functions = get_kpi_functions(current_module)

        # Filter valid records
        valid_records = [
            r
            for r in model.unified_result_records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]

        if not valid_records:
            return out

        # Group records by test path for 2D KPIs (same test, different rates)
        from collections import defaultdict

        records_by_test = defaultdict(list)
        for r in valid_records:
            records_by_test[r.test_base_path].append(r)

        # Generate scalar KPIs for each record
        for r in valid_records:
            base_labels = {**r.distinguishing_labels}
            test_condition_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(r)
            metadata_fields = GuideLLMKpiHandler.extract_metadata(r)

            # Compute scalar KPIs only
            for kpi_id, kpi_func in kpi_functions.items():
                # Skip 2D KPIs for individual records - they'll be handled separately
                if is_2d_kpi(kpi_func):
                    continue

                try:
                    value = kpi_func(r)
                except (TypeError, ValueError, KeyError):
                    value = None  # None for missing/failed scalar KPIs

                # Skip KPIs with null values
                if value is None:
                    continue

                # Merge base labels, test condition labels, and system labels
                all_labels = {
                    **base_labels,
                    **test_condition_labels,
                    "higher_is_better": kpi_func._kpi_higher_is_better,
                }

                kpi_record = {
                    "schema_version": "1",
                    "kpi_id": kpi_id,
                    "value": value,
                    "unit": kpi_func._kpi_unit,
                    "run_id": r.test_base_path,
                    "timestamp": ts,
                    "labels": all_labels,
                    "metadata": metadata_fields,
                    "source": {
                        "test_base_path": r.test_base_path,
                        "plugin_module": model.plugin_module,
                    },
                    "is_2d": False,
                }

                out.append(kpi_record)

        # Generate 2D curve KPIs for records that have performance curves
        for r in valid_records:
            # Check if this record has performance curves (indicating it's aggregated data)
            curves = r.metrics.get("performance_curves", {})
            request_rates = r.metrics.get("request_rate", [])

            # Only generate 2D KPIs if we have performance curves with data
            if not curves or not request_rates:
                continue

            base_labels = {**r.distinguishing_labels}
            test_condition_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(r)
            metadata_fields = GuideLLMKpiHandler.extract_metadata(r)

            # Generate 2D KPIs from performance curves
            for kpi_id, kpi_func in kpi_functions.items():
                if not is_2d_kpi(kpi_func):
                    continue

                try:
                    # Pass the single record with performance curves to the 2D KPI function
                    value = kpi_func(r)
                except (TypeError, ValueError, KeyError):
                    value = []  # Empty list for failed 2D KPIs

                # Skip 2D KPIs with empty or null values
                if not value or value is None:
                    continue

                # Remove rate-specific labels for aggregated KPIs
                aggregated_labels = {
                    k: v
                    for k, v in base_labels.items()
                    if k not in ["concurrency", "rate", "max_concurrency"]
                }
                all_labels = {
                    **aggregated_labels,
                    **test_condition_labels,
                    "higher_is_better": kpi_func._kpi_higher_is_better,
                }

                kpi_record = {
                    "schema_version": "1",
                    "kpi_id": kpi_id,
                    "value": value,
                    "unit": kpi_func._kpi_unit,
                    "run_id": r.test_base_path,
                    "timestamp": ts,
                    "labels": all_labels,
                    "metadata": metadata_fields,
                    "source": {
                        "test_base_path": r.test_base_path,
                        "plugin_module": model.plugin_module,
                    },
                    "is_2d": True,
                    "x_unit": kpi_func._kpi_x_unit,
                    "x_help": kpi_func._kpi_x_help,
                    "y_unit": getattr(kpi_func, "_kpi_y_unit", None) or kpi_func._kpi_unit,
                    "y_help": getattr(kpi_func, "_kpi_y_help", None) or kpi_func._kpi_help,
                }

                out.append(kpi_record)

        return out
