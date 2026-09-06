from __future__ import annotations

from datetime import UTC, datetime

from projects.caliper.engine.kpi import (
    KpiCatalogEntry,
    KpiComputationStatus,
    KpiRecord,
    build_catalog_from_functions,
    get_kpi_functions,
    is_curve_kpi,
)
from projects.caliper.engine.model import UnifiedRunModel
from projects.guidellm.postprocess.guidellm.dashboard import (
    compute_dashboard_kpis,
    dashboard_kpi_catalog,
)


class RhaiisKpiHandler:
    @staticmethod
    def get_catalog() -> list[KpiCatalogEntry]:
        """Get KPI catalog entries for RHAIIS dashboards."""
        # Get dashboard scalar KPIs
        dashboard_catalog = dashboard_kpi_catalog(prefix="rhaiis")

        # Get curve KPI catalog
        from . import curve_kpis

        curve_catalog = build_catalog_from_functions(curve_kpis)

        # Combine both catalogs
        return dashboard_catalog + curve_catalog

    @staticmethod
    def compute_kpis(model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute both scalar dashboard KPIs and curve KPIs from unified run model."""
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_kpis: list[KpiRecord] = []

        # 1. Get dashboard scalar KPIs (per rate point)
        dashboard_kpis = compute_dashboard_kpis(model, prefix="rhaiis")
        all_kpis.extend(dashboard_kpis)

        # 2. Generate curve KPIs from performance curves
        from . import curve_kpis

        kpi_functions = get_kpi_functions(curve_kpis)

        valid_records = [
            r
            for r in model.unified_result_records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]

        processed_records = 0

        for record in valid_records:
            # Check if this record has performance curves
            curves = record.metrics.get("performance_curves", {})
            if not curves:
                continue

            # Track that we processed this record
            record_had_kpis = False

            # Extract labels from the record
            base_labels = {**record.distinguishing_labels}
            metadata = {"run_path": record.test_base_path}

            # Generate curve KPIs
            for kpi_id, kpi_func in kpi_functions.items():
                if not is_curve_kpi(kpi_func):
                    continue

                try:
                    # Get curve data from function
                    raw_values = kpi_func(record)
                    # Convert to [[x, y], [x, y]] format
                    values = [[float(x), float(y)] for x, y in raw_values] if raw_values else []
                except (TypeError, ValueError, KeyError):
                    values = []

                # Skip empty curves
                if not values:
                    continue

                # Create curve KPI record
                curve_kpi = KpiRecord(
                    kpi_id=kpi_id,
                    values=values,
                    run_id=record.test_base_path,
                    timestamp=ts,
                    labels=base_labels,
                    metadata=metadata,
                    is_curve=True,
                    higher_is_better=kpi_func._kpi_higher_is_better,
                    unit=kpi_func._kpi_unit,
                    x_unit=kpi_func._kpi_x_unit,
                    x_help=kpi_func._kpi_x_help,
                    y_unit=kpi_func._kpi_y_unit,
                    y_help=kpi_func._kpi_y_help,
                )

                all_kpis.append(curve_kpi)
                record_had_kpis = True

            if record_had_kpis:
                processed_records += 1

        status = KpiComputationStatus.success_status(
            processed_records, len(model.unified_result_records)
        )
        return all_kpis, status
