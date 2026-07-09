"""GuideLLM Caliper PostProcessingPlugin (`projects/caliper/postprocess/guidellm`)."""

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

from .ai_eval import GuideLLMAIEvaluator
from .parsing import GuideLLMKpiHandler, GuideLLMParser
from .plotting.kpi_report import generate_kpi_report
from .plotting.performance_analysis import generate_comprehensive_performance_report

logger = logging.getLogger(__name__)


# Plot registry - maps report names to their generator functions and parameters
PLOT_REGISTRY = {
    "report_performance_analysis": {
        "function": generate_comprehensive_performance_report,
        "type": "report",
        "kwargs": {
            "report_number": 0,
            "report_title": "GuideLLM Performance Analysis",
        },
        "description": "comprehensive performance analysis report (recommended)",
    },
    "report_kpi_summary": {
        "function": generate_kpi_report,
        "type": "report",
        "kwargs": {
            "report_number": 1,
            "report_title": "GuideLLM KPI Summary",
        },
        "description": "KPI summary with test conditions and metrics",
    },
}


class GuideLLMPlugin(PostProcessingPlugin):
    """
    Parses GuideLLM benchmark artifacts containing ``benchmarks.json`` files.
    """

    def __init__(self):
        self.parser = GuideLLMParser()
        self.kpi_handler = GuideLLMKpiHandler()
        self.ai_evaluator = GuideLLMAIEvaluator()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        """Parse test nodes using the GuideLLM parser."""
        return self.parser.parse(nodes)

    def get_available_reports(self) -> dict[str, dict[str, str]]:
        """Get a structured dictionary of available reports and plots with their types and descriptions."""
        return {
            name: {
                "type": config["type"],
                "description": config["description"],
            }
            for name, config in PLOT_REGISTRY.items()
        }

    def get_available_reports_by_type(self) -> dict[str, dict[str, str]]:
        """Get reports and plots grouped by type."""
        result = {"reports": {}, "plots": {}}
        for name, config in PLOT_REGISTRY.items():
            type_key = "reports" if config["type"] == "report" else "plots"
            result[type_key][name] = config["description"]
        return result

    def get_reports_only(self) -> dict[str, str]:
        """Get only comprehensive reports (HTML files with multiple plots)."""
        return {
            name: config["description"]
            for name, config in PLOT_REGISTRY.items()
            if config["type"] == "report"
        }

    def get_plots_only(self) -> dict[str, str]:
        """Get only individual plots (single visualizations)."""
        return {
            name: config["description"]
            for name, config in PLOT_REGISTRY.items()
            if config["type"] == "plot"
        }

    @staticmethod
    def register_plot(
        name: str, function: callable, description: str, type_: str = "plot", **kwargs
    ) -> None:
        """Register a new plot or report generator function.

        Args:
            name: Report name (used with --reports)
            function: Generator function that takes (records, output_dir, **kwargs)
            description: Human-readable description for help text
            type_: Type of visualization ("plot" for single chart, "report" for comprehensive HTML)
            **kwargs: Additional arguments passed to the function

        Example:
            GuideLLMPlugin.register_plot(
                "custom_analysis",
                my_custom_function,
                "My custom analysis",
                type_="plot",
                report_number=10
            )
        """
        PLOT_REGISTRY[name] = {
            "function": function,
            "type": type_,
            "description": description,
            "kwargs": kwargs,
        }

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[str]:
        """Generate visualization reports for GuideLLM benchmarks."""
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        wanted = frozenset(report_ids or ())

        # Filter to only GuideLLM records with benchmarks
        guidellm_records = [
            r
            for r in model.unified_result_records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]

        if not guidellm_records:
            return paths

        # Generate reports using the registry
        for report_name in wanted:
            if report_name not in PLOT_REGISTRY:
                logger.warning("Unknown report '%s' requested", report_name)
                continue

            plot_config = PLOT_REGISTRY[report_name]
            function = plot_config["function"]
            kwargs = plot_config.get("kwargs", {})

            try:
                # Call the generator function with records, output_dir, and any additional kwargs
                path = function(guidellm_records, output_dir, **kwargs)
                if path:
                    paths.append(path)
                    logger.info("Generated %s: %s", report_name, path)
                else:
                    logger.warning("Failed to generate %s", report_name)
            except Exception as e:
                logger.error("Error generating %s: %s", report_name, e)

        return paths

    def kpi_catalog(self) -> list[dict[str, Any]]:
        """Return the GuideLLM KPI catalog."""
        return self.kpi_handler.get_catalog()

    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, Any]]:
        """Compute KPI values from the unified model."""
        return self.kpi_handler.compute_kpis(model)

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, Any]:
        """Build AI evaluation payload from the unified model."""
        return self.ai_evaluator.build_payload(model, self)

    def get_ai_data_artifact_files_for_test(self, test_dir: Path) -> list[str]:
        """Return list of artifact files to copy for AI evaluation export from a specific test directory.

        Args:
            test_dir: The specific test directory to search within

        Returns:
            List of relative paths from test_dir to relevant artifact files
        """
        artifact_files = []

        # LLMInferenceService state files - search within this test directory only
        llmisvc_patterns = [
            "*__capture_llmisvc_state/artifacts/llminferenceservice.json",
            "*__capture_llmisvc_state/artifacts/llminferenceservice.deployments.json",
        ]

        for pattern in llmisvc_patterns:
            matches = list(test_dir.glob(pattern))
            if matches:
                logger.debug(
                    f"Found {len(matches)} LLMInferenceService files in {test_dir} for pattern {pattern}"
                )
                for match in matches:
                    relative_path = str(match.relative_to(test_dir))
                    artifact_files.append(relative_path)
                    logger.debug(f"Found LLMInferenceService artifact: {relative_path}")
            else:
                logger.debug(f"No matches found in {test_dir} for pattern: {pattern}")

        # GuideLLM benchmark results - search within this test directory only
        benchmark_pattern = "**/*__run_guidellm_benchmark/artifacts/results/benchmarks*.json"
        benchmark_matches = list(test_dir.glob(benchmark_pattern))

        if benchmark_matches:
            logger.debug(f"Found {len(benchmark_matches)} GuideLLM benchmark files in {test_dir}")
            for match in benchmark_matches:
                relative_path = str(match.relative_to(test_dir))
                artifact_files.append(relative_path)
                logger.debug(f"Found GuideLLM benchmark artifact: {relative_path}")
        else:
            logger.debug(f"No benchmark files found in {test_dir}")

        # Project configuration file - include if present
        config_file = test_dir / "config.yaml"
        if config_file.exists():
            relative_path = str(config_file.relative_to(test_dir))
            artifact_files.append(relative_path)
            logger.info(f"Found project config artifact: {relative_path}")
        else:
            logger.warning(f"No config.yaml found in {test_dir}")

        logger.info(
            f"AI eval export will copy {len(artifact_files)} artifact files from {test_dir}"
        )
        return artifact_files


def get_plugin() -> PostProcessingPlugin:
    """Return the GuideLLM plugin instance."""
    return GuideLLMPlugin()
