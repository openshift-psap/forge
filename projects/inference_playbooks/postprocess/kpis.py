"""KPI computation for Inference Playbooks benchmarks."""

from __future__ import annotations

from projects.aiperf.postprocess.results import compute_aiperf_kpis, model_for_tool
from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.model import UnifiedRunModel
from projects.guidellm.postprocess.guidellm.dashboard import (
    compute_dashboard_kpis,
    dashboard_kpi_catalog,
)


class PlaybooksKpiHandler:
    @staticmethod
    def get_catalog() -> list[KpiCatalogEntry]:
        return dashboard_kpi_catalog(prefix="playbooks")

    @staticmethod
    def compute_kpis(
        model: UnifiedRunModel,
    ) -> tuple[list[KpiRecord], KpiComputationStatus]:
        guidellm_model = model_for_tool(model, "guidellm")
        guidellm_kpis = compute_dashboard_kpis(guidellm_model, prefix="playbooks")
        aiperf_kpis, _ = compute_aiperf_kpis(model)
        all_kpis = guidellm_kpis + aiperf_kpis
        return all_kpis, KpiComputationStatus.success_status(len(model.unified_result_records))
