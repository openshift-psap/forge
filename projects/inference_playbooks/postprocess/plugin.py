"""Inference Playbooks Caliper PostProcessingPlugin.

Owns the postprocessing contract for inference-playbooks runs that may
include GuideLLM benchmarks, AI Perf benchmarks, or both sequentially.

Reuses existing GuideLLM and AI Perf parsing/KPI helpers through
composition—not by subclassing another project's plugin.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from projects.aiperf.postprocess.results import (
    AIPerfParser,
    benchmark_tool,
    model_for_tool,
)
from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.kpi.analyze import AnalysisConfig
from projects.caliper.engine.model import (
    BaseTestNode,
    ParseResult,
    PostProcessingPlugin,
    UnifiedRunModel,
)
from projects.guidellm.postprocess.guidellm.dashboard import enrich_guidellm_parse_result
from projects.guidellm.postprocess.guidellm.parsing import GuideLLMParser

from .csv_dashboard import PLAYBOOKS_FIELDNAMES, playbooks_metadata_row_mapper
from .kpis import PlaybooksKpiHandler

logger = logging.getLogger(__name__)

analysis_config = AnalysisConfig(
    comparison_labels=["product_version"],
    ignored_labels=["higher_is_better", "sweep_id"],
    sorting_labels=["product_version"],
    regression_config={
        "SCALAR_RELATIVE_CHANGE": {
            "max_relative_regression": 0.15,
            "min_baseline_points": 2,
        },
    },
)


class InferencePlaybooksPlugin(PostProcessingPlugin):
    """Parse and compute KPIs for mixed GuideLLM + AI Perf benchmark runs."""

    def __init__(self) -> None:
        self._guidellm_parser = GuideLLMParser()
        self._aiperf_parser = AIPerfParser()
        self._kpi_handler = PlaybooksKpiHandler()

    def parse(self, nodes: list[BaseTestNode]) -> ParseResult:
        guidellm_nodes = [n for n in nodes if benchmark_tool(n) == "guidellm"]
        aiperf_nodes = [n for n in nodes if benchmark_tool(n) == "aiperf"]

        guidellm_result = enrich_guidellm_parse_result(
            self._guidellm_parser.parse(guidellm_nodes), guidellm_nodes
        )
        aiperf_result = self._aiperf_parser.parse(aiperf_nodes)

        return ParseResult(
            records=guidellm_result.records + aiperf_result.records,
            warnings=guidellm_result.warnings + aiperf_result.warnings,
        )

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        return self._kpi_handler.compute_kpis(model)

    def kpi_catalog(self) -> list[KpiCatalogEntry]:
        return self._kpi_handler.get_catalog()

    def export_dashboard_csv(self, model: UnifiedRunModel, output_path: Path) -> str:
        guidellm_model = model_for_tool(model, "guidellm")
        if not guidellm_model.unified_result_records:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", newline="", encoding="utf-8") as stream:
                csv.DictWriter(stream, fieldnames=PLAYBOOKS_FIELDNAMES).writeheader()
            return str(output_path)

        from projects.guidellm.postprocess.guidellm.dashboard import DashboardCsvExporter

        exporter = DashboardCsvExporter()
        return exporter.export_dashboard_csv(
            guidellm_model,
            output_path,
            prefix="playbooks",
            fieldnames=PLAYBOOKS_FIELDNAMES,
            metadata_row_mapper=playbooks_metadata_row_mapper,
        )

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, Any]:
        entries = []
        for idx, record in enumerate(model.unified_result_records):
            tool = "aiperf" if record.run_identity.get("aiperf") else "guidellm"
            entry: dict[str, Any] = {
                "test_id": f"test_entry_{idx:03d}",
                "test_base_path": record.test_base_path,
                "benchmark_tool": tool,
                "distinguishing_labels": record.distinguishing_labels,
            }
            if tool == "aiperf":
                entry["metrics"] = record.metrics.get("aiperf_summary", {})
            else:
                curves = record.metrics.get("performance_curves", {})
                entry["metrics"] = {
                    "strategy": record.metrics.get("strategy", "unknown"),
                    "rate_points": len(curves.get("request_rate", [])),
                }
                entry["performance_curves"] = {
                    k: curves.get(k, [])
                    for k in (
                        "request_rate",
                        "tokens_per_second",
                        "ttft_median",
                        "ttft_p95",
                        "tpot_median",
                        "tpot_p95",
                    )
                }
            entries.append(entry)

        return {
            "schema_version": "1",
            "run_id": str(model.base_directory),
            "test_entries": entries,
        }

    def get_ai_data_artifact_files_for_test(self, test_dir: Path) -> list[str]:
        files: list[str] = []
        for pattern in (
            "**/*__run_guidellm_benchmark/artifacts/results/benchmarks*.json",
            "**/*__run_aiperf_benchmark/artifacts/aiperf/profile_export_aiperf.json",
            "config.yaml",
        ):
            files.extend(str(p.relative_to(test_dir)) for p in test_dir.glob(pattern))
        return files


def get_plugin() -> PostProcessingPlugin:
    return InferencePlaybooksPlugin()
