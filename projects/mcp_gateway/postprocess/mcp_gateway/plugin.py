"""MCP Gateway Caliper PostProcessingPlugin."""

from __future__ import annotations

import logging
from typing import Any

from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)

from .parsing import MCPGatewayKpiHandler, MCPGatewayParser

logger = logging.getLogger(__name__)


class MCPGatewayPlugin(PostProcessingPlugin):
    """Parses Locust stats.csv artifacts from MCP Gateway performance tests."""

    def __init__(self):
        self.parser = MCPGatewayParser()
        self.kpi_handler = MCPGatewayKpiHandler()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        return self.parser.parse(nodes)

    def kpi_catalog(self) -> list[dict[str, Any]]:
        return self.kpi_handler.get_catalog()

    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
        return self.kpi_handler.compute_kpis(model)


def get_plugin() -> PostProcessingPlugin:
    """Return the MCP Gateway plugin instance."""
    return MCPGatewayPlugin()
