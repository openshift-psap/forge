"""KPI definitions and computation for Skeleton Caliper plugin."""

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


# Throughput KPIs
@HigherBetter()
@Format("{:.2f}")
@KPIMetadata(help="Number of requests processed per second", unit="req/s")
def kpi_skeleton_throughput_rps(unified_record) -> float:
    """Throughput KPI."""
    raw_value = unified_record.metrics.get("throughput")
    if raw_value is None:
        raise ValueError("throughput metric not found")
    return float(raw_value)


# Latency KPIs
@LowerBetter()
@Format("{:.2f}")
@KPIMetadata(help="Average response latency in milliseconds", unit="ms")
def kpi_skeleton_latency_ms(unified_record) -> float:
    """Latency KPI."""
    raw_value = unified_record.metrics.get("latency_ms")
    if raw_value is None:
        raise ValueError("latency_ms metric not found")
    return float(raw_value)


class SkeletonKpiHandler:
    """Handles KPI catalog and computation for skeleton project."""

    # Create label extractor for test condition labels
    LABEL_EXTRACTOR = create_label_extractor(
        {
            "scenario": "distinguishing_labels.scenario",
            "workload": "distinguishing_labels.workload",
        }
    )

    @staticmethod
    def extract_metadata(record) -> dict[str, Any]:
        """Extract metadata fields from test record."""
        return {
            "test_config": record.run_identity.get("test_config", {}),
            "environment": record.run_identity.get("environment", "unknown"),
        }

    @staticmethod
    def get_catalog() -> list[dict[str, Any]]:
        """
        Return the KPI catalog for skeleton metrics.

        Returns:
            List of KPI definitions
        """
        current_module = inspect.getmodule(SkeletonKpiHandler)
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
        current_module = inspect.getmodule(SkeletonKpiHandler)
        kpi_functions = get_kpi_functions(current_module)

        for r in model.unified_result_records:
            base_labels = {**r.distinguishing_labels}

            # Extract test condition labels (same for all KPIs in this test)
            test_condition_labels = SkeletonKpiHandler.LABEL_EXTRACTOR.extract(r)

            # Extract metadata fields
            metadata_fields = SkeletonKpiHandler.extract_metadata(r)

            # Compute each KPI using the decorated functions
            for kpi_id, kpi_func in kpi_functions.items():
                try:
                    value = kpi_func(r)
                except (TypeError, ValueError, KeyError):
                    if is_2d_kpi(kpi_func):
                        value = []  # Empty list for failed 2D KPIs
                    else:
                        value = None  # None for missing/failed scalar KPIs

                # Skip KPIs with null/empty values
                if value is None or (isinstance(value, list) and not value):
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
                }

                # Add 2D-specific metadata
                if is_2d_kpi(kpi_func):
                    kpi_record.update(
                        {
                            "x_unit": kpi_func._kpi_x_unit,
                            "x_help": kpi_func._kpi_x_help,
                            "y_unit": getattr(kpi_func, "_kpi_y_unit", None) or kpi_func._kpi_unit,
                            "y_help": getattr(kpi_func, "_kpi_y_help", None) or kpi_func._kpi_help,
                        }
                    )

                out.append(kpi_record)

        return out
