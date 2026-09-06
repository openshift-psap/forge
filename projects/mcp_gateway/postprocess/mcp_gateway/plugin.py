"""MCP Gateway Caliper PostProcessingPlugin."""

from __future__ import annotations

import logging

from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.kpi.analyze import AnalysisConfig
from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)

from .parsing import MCPGatewayKpiHandler, MCPGatewayParser

logger = logging.getLogger(__name__)

# Compare versions while matching on load shape / target / protocol.
analysis_config = AnalysisConfig(
    comparison_labels=["mcp_gateway_version"],
    ignored_labels=[],
    sorting_labels=["num_servers", "users", "target"],
    regression_config={
        "SCALAR_RELATIVE_CHANGE": {
            "max_relative_regression": 0.10,
            "min_baseline_points": 1,
        },
    },
)


class MCPGatewayPlugin(PostProcessingPlugin):
    """Parses Locust stats.csv artifacts from MCP Gateway performance tests."""

    def __init__(self):
        self.parser = MCPGatewayParser()
        self.kpi_handler = MCPGatewayKpiHandler()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        return self.parser.parse(nodes)

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        return self.kpi_handler.compute_kpis(model)

    def kpi_catalog(self) -> list[KpiCatalogEntry]:
        """Return catalog of available KPIs for hierarchical formatting."""
        return self.kpi_handler.get_catalog()


def get_plugin() -> PostProcessingPlugin:
    """Return the MCP Gateway plugin instance."""
    return MCPGatewayPlugin()
