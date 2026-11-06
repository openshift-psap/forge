"""Sample Caliper PostProcessingPlugin for Skeleton (`projects/skeleton/postprocess/default`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.kpi.analyze import AnalysisConfig
from projects.caliper.engine.model import (
    BaseTestNode,
    ParseResult,
    PostProcessingPlugin,
    ReportFile,
    UnifiedRunModel,
)

from .parsing import SkeletonKpiHandler, SkeletonParser
from .plotting import SummaryTablePlot, ThroughputChartPlot

logger = logging.getLogger(__name__)


class SkeletonDefaultPlugin(PostProcessingPlugin):
    """
    Parses per-test directories containing ``metrics.json`` (simple numeric mapping).

    Visual reports (Plotly HTML):

    * ``summary_table`` — tabular view of scenarios and metrics.
    * ``throughput_chart`` — bar chart of ``throughput`` when present.
    """

    def __init__(self):
        self.parser = SkeletonParser()
        self.kpi_handler = SkeletonKpiHandler()
        self.plots = {
            "summary_table": SummaryTablePlot,
            "throughput_chart": ThroughputChartPlot,
        }

    def parse(self, nodes: list[BaseTestNode]) -> ParseResult:
        """Parse test nodes using the skeleton parser."""
        return self.parser.parse(nodes)

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[ReportFile]:
        """Generate visualization reports."""
        logger.info("Starting skeleton plugin visualization")
        source_dir = output_dir / "global" / "skeleton"
        source_dir.mkdir(parents=True, exist_ok=True)
        reports: list[ReportFile] = []
        wanted = frozenset(report_ids or ())
        invalid_plots: list[str] = []

        for report_id in wanted:
            if report_id in self.plots:
                plot_class = self.plots[report_id]
                path = plot_class.generate(model, source_dir)
                reports.append(
                    ReportFile(
                        source="skeleton",
                        scope="global",
                        title=report_id,
                        path=path,
                    )
                )
            else:
                logger.warning(f"Requested plot type '{report_id}' not available in this plugin")
                invalid_plots.append(report_id)

        if invalid_plots:
            available_types = list(self.plots.keys())
            raise ValueError(
                f"Invalid plot types requested: {sorted(invalid_plots)}. "
                f"Available types in this plugin: {sorted(available_types)}"
            )

        logger.info(f"Skeleton plugin visualization completed. Generated {len(reports)} files")
        return reports

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute KPI values from the unified model."""
        return self.kpi_handler.compute_kpis(model)

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, Any]:
        """Build AI evaluation payload from the unified model."""
        return {
            "schema_version": "1",
            "run_id": model.base_directory,
            "metrics": {
                "record_count": len(model.unified_result_records),
                "scenarios": [
                    str(r.distinguishing_labels.get("scenario", r.test_base_path))
                    for r in model.unified_result_records
                ],
            },
            "optional": {},
        }

    def kpi_catalog(self) -> list[KpiCatalogEntry]:
        """Return catalog of available KPIs for hierarchical formatting."""
        return self.kpi_handler.get_catalog()


# Analysis configuration for KPI regression analysis
analysis_config = AnalysisConfig(
    comparison_labels=["version"],  # Compare across different versions (dates in YYYY-MM-DD format)
    ignored_labels=["higher_is_better"],  # Ignore KPI metadata labels
    regression_config={
        "SCALAR_RELATIVE_CHANGE": {
            "max_relative_regression": 0.1,  # 10% threshold
            "min_baseline_points": 1,  # Minimum baseline points needed
        },
    },
)


def get_plugin() -> PostProcessingPlugin:
    """Return the skeleton plugin instance."""
    return SkeletonDefaultPlugin()
