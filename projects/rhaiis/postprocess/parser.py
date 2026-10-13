from __future__ import annotations

from projects.caliper.engine.model import BaseTestNode, ParseResult
from projects.guidellm.postprocess.guidellm.dashboard import enrich_guidellm_parse_result
from projects.guidellm.postprocess.guidellm.parsing.parsers import GuideLLMParser


class RhaiisParser:
    """Parse GuideLLM results and retain dashboard-specific metrics."""

    def __init__(self) -> None:
        self._base_parser = GuideLLMParser()

    def parse(self, nodes: list[BaseTestNode]) -> ParseResult:
        return enrich_guidellm_parse_result(self._base_parser.parse(nodes), nodes)
