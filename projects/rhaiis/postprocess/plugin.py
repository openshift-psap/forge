from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)

from .kpis import RhaiisKpiHandler
from .parser import RhaiisParser

logger = logging.getLogger(__name__)


class RhaiisPlugin(PostProcessingPlugin):
    def __init__(self) -> None:
        self.parser = RhaiisParser()
        self.kpi_handler = RhaiisKpiHandler()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        return self.parser.parse(nodes)

    def get_available_reports(self) -> dict[str, dict[str, str]]:
        return {}

    def get_available_reports_by_type(self) -> dict[str, dict[str, str]]:
        return {"reports": {}, "plots": {}}

    def get_reports_only(self) -> dict[str, str]:
        return {}

    def get_plots_only(self) -> dict[str, str]:
        return {}

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[str]:
        return []

    def kpi_catalog(self) -> list[dict[str, Any]]:
        return self.kpi_handler.get_catalog()

    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
        return self.kpi_handler.compute_kpis(model)

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, Any]:
        return {}


def get_plugin() -> PostProcessingPlugin:
    return RhaiisPlugin()
