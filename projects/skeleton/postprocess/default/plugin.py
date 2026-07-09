"""Sample Caliper PostProcessingPlugin for Skeleton (`projects/skeleton/postprocess/default`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)

from .parsing import SkeletonKpiHandler, SkeletonParser
from .plotting import SummaryTablePlot, ThroughputChartPlot


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

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        """Parse test nodes using the skeleton parser."""
        return self.parser.parse(nodes)

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[str]:
        """Generate visualization reports."""
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        wanted = frozenset(report_ids or ())

        for report_id in wanted:
            if report_id in self.plots:
                plot_class = self.plots[report_id]
                path = plot_class.generate(model, output_dir)
                paths.append(path)

        return paths

    def kpi_catalog(self) -> list[dict[str, Any]]:
        """Return the KPI catalog."""
        return self.kpi_handler.get_catalog()

    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
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


def get_plugin() -> PostProcessingPlugin:
    """Return the skeleton plugin instance."""
    return SkeletonDefaultPlugin()
