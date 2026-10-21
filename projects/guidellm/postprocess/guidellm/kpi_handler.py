"""KPI handler for GuideLLM benchmarks."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from projects.caliper.engine.kpi import (
    KpiCatalogEntry,
    KpiComputationStatus,
    KpiRecord,
    build_catalog_from_functions,
    get_kpi_functions,
    is_curve_kpi,
)
from projects.caliper.engine.model import UnifiedRunModel

logger = logging.getLogger(__name__)


class GuideLLMKpiHandler:
    """Handles KPI catalog and computation for GuideLLM benchmarks."""

    REQUIRED_LABELS = {
        "model_name",
        "guidellm_loadshape",
    }

    @staticmethod
    def _extract_labels(record) -> dict[str, Any]:
        """Extract labels from record with fallbacks for missing kpi_labels."""

        from projects.caliper.engine.kpi.decorators import _extract_value_by_path

        labels = {}

        artifact_fields = {
            "product_version": "metrics.product_version",
            "cluster": "metrics.cluster",
            "deployment_profile": "metrics.deployment_profile",
            "guidellm_loadshape": "metrics.benchmark_key",
        }

        for label_key, path in artifact_fields.items():
            value = _extract_value_by_path(record, path)
            if value is not None:
                labels[label_key] = str(value)

        labels |= record.metrics.get("kpi_labels", {}) or {}

        return labels

    def __init__(self, *, required_labels: set[str] | None = None):
        self._required_labels = required_labels or self.REQUIRED_LABELS

    def _validate_required_labels(self, labels: dict[str, Any]) -> set[str]:
        """Validate required labels and return any missing labels."""
        return self._required_labels - labels.keys()

    LABEL_EXTRACTOR = type("TestLabelExtractor", (), {"extract": _extract_labels})()

    @staticmethod
    def extract_metadata(record) -> dict[str, Any]:
        """Extract metadata fields for KPI records."""
        config = record.metrics.get("configuration", {})
        return {
            "configuration": config,
            "run_path": record.test_base_path,
        }

    @staticmethod
    def get_catalog() -> list[KpiCatalogEntry]:
        """Return the KPI catalog for GuideLLM metrics using dataclasses."""
        from projects.guidellm.postprocess.guidellm.parsing import kpis as guidellm_kpis

        return build_catalog_from_functions(guidellm_kpis)

    def compute_kpis(
        self,
        model: UnifiedRunModel,
    ) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute KPI values from the unified model."""
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[KpiRecord] = []

        from projects.guidellm.postprocess.guidellm.parsing import kpis as guidellm_kpis

        kpi_functions = get_kpi_functions(guidellm_kpis)

        valid_records = [
            r
            for r in model.unified_result_records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]

        if not valid_records:
            status = KpiComputationStatus.success_status(0, len(model.unified_result_records))
            return out, status

        unknown_gpu_records = []
        for r in valid_records:
            test_condition_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(r)
            gpu_type = test_condition_labels.get("gpu_type")
            if gpu_type and gpu_type.lower() == "unknown":
                unknown_gpu_records.append(r.test_base_path)

        if unknown_gpu_records:
            error_msg = (
                f"Found gpu_type='unknown' in {len(unknown_gpu_records)} test paths: "
                f"{', '.join(unknown_gpu_records[:3])}{'...' if len(unknown_gpu_records) > 3 else ''}. "
                "Unknown GPU types prevent reliable KPI analysis. "
                "Please ensure GPU detection is working correctly or set gpu_type manually in test labels."
            )
            status = KpiComputationStatus.failure_status(error_msg)
            return [], status

        invalid_label_records = []
        logger.info(f"🔍 Validating required labels for {len(valid_records)} records...")
        logger.info(f"   Required labels: {sorted(self._required_labels)}")

        for r in valid_records:
            test_condition_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(r)
            missing_labels = self._validate_required_labels(test_condition_labels)

            logger.debug(f"   Record {r.test_base_path}:")
            logger.debug(f"      Available: {sorted(test_condition_labels.keys())}")
            logger.debug(f"      Missing: {sorted(missing_labels) if missing_labels else 'None'}")

            if missing_labels:
                invalid_label_records.append(
                    {
                        "test_path": r.test_base_path,
                        "missing_labels": sorted(missing_labels),
                        "available_labels": sorted(test_condition_labels.keys()),
                    }
                )
                logger.warning(f"   ❌ {r.test_base_path}: missing {sorted(missing_labels)}")

        logger.info(f"   Validation complete: {len(invalid_label_records)} invalid records found")

        records_by_test = defaultdict(list)
        for r in valid_records:
            records_by_test[r.test_base_path].append(r)

        processed_records = set()

        for r in valid_records:
            test_condition_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(r)

            missing_labels = self._validate_required_labels(test_condition_labels)
            if missing_labels:
                continue

            metadata_fields = GuideLLMKpiHandler.extract_metadata(r)

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
                    **test_condition_labels,
                }

                kpi_record = KpiRecord(
                    kpi_id=kpi_id,
                    value=value,
                    unit=kpi_func._kpi_unit,
                    run_id=r.test_base_path,
                    timestamp=ts,
                    labels=all_labels,
                    metadata=metadata_fields,
                    is_curve=False,
                )

                out.append(kpi_record)
                processed_records.add(r.test_base_path)

        for r in valid_records:
            curves = r.metrics.get("performance_curves", {})
            request_rates = r.metrics.get("request_rate", [])

            if not curves or not request_rates:
                continue

            kpi_labels = GuideLLMKpiHandler.LABEL_EXTRACTOR.extract(r)

            missing_labels = self._validate_required_labels(kpi_labels)
            if missing_labels:
                continue

            metadata_fields = GuideLLMKpiHandler.extract_metadata(r)

            for kpi_id, kpi_func in kpi_functions.items():
                if not is_curve_kpi(kpi_func):
                    continue

                raw_values = kpi_func(r)
                if raw_values:
                    coordinate_pairs = [[x, y] for x, y in raw_values]
                    values = sorted(coordinate_pairs, key=lambda point: point[0])
                else:
                    values = []

                if not values or values is None:
                    continue

                kpi_record = KpiRecord(
                    schema_version="1",
                    kpi_id=kpi_id,
                    values=values,
                    unit=kpi_func._kpi_unit,
                    run_id=r.test_base_path,
                    timestamp=ts,
                    labels=kpi_labels,
                    metadata=metadata_fields,
                    is_curve=True,
                    x_unit=kpi_func._kpi_x_unit,
                    x_help=kpi_func._kpi_x_help,
                    y_unit=kpi_func._kpi_y_unit,
                    y_help=kpi_func._kpi_y_help,
                )

                out.append(kpi_record)
                processed_records.add(r.test_base_path)

        if invalid_label_records:
            error_msg = (
                f"Found missing required labels in {len(invalid_label_records)} test paths:\n"
                + "\n".join(
                    [
                        f"  - {r['test_path']}: missing {r['missing_labels']}, available {r['available_labels']}"
                        for r in invalid_label_records[:3]
                    ]
                )
                + ("..." if len(invalid_label_records) > 3 else "")
                + "\nPlease ensure all required labels are set in the KPI labels configuration."
            )
            status = KpiComputationStatus.failure_status(error_msg)
            logger.error("❌ KPI computation failed due to missing required labels")
            logger.error(f"   Generated {len(out)} KPIs before failing")
            return out, status

        status = KpiComputationStatus.success_status(len(processed_records), len(valid_records))
        logger.info(
            f"✅ KPI computation successful: {len(out)} KPIs generated from {len(processed_records)} records"
        )
        return out, status
