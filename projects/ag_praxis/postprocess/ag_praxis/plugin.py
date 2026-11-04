from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import BaseTestNode, UnifiedRunModel
from projects.caliper.postprocess.prometheus.parser import parse_capture_group
from projects.caliper.postprocess.prometheus.report import generate_group_report
from projects.guidellm.postprocess.guidellm.plugin import PLOT_REGISTRY, GuideLLMPlugin

logger = logging.getLogger(__name__)

PROMETHEUS_REPORT_ID = "report_prometheus_metrics"


class AgPraxisPlugin(GuideLLMPlugin):
    def __init__(self):
        super().__init__()
        PLOT_REGISTRY[PROMETHEUS_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "Prometheus metrics time-series reports from capture artifacts",
        }

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[str]:
        wanted = list(dict.fromkeys(report_ids or ()))
        prom_requested = PROMETHEUS_REPORT_ID in wanted
        parent_ids = [r for r in wanted if r != PROMETHEUS_REPORT_ID] or None

        paths = super().visualize(model, output_dir, parent_ids, group_id, visualize_config)

        if not prom_requested:
            return paths

        prom_paths = _generate_prometheus_reports(model, output_dir / "prometheus")
        paths.extend(prom_paths)

        if not prom_paths:
            logger.info("No Prometheus capture data found in test artifacts")

        return paths


def _generate_prometheus_reports(model: UnifiedRunModel, output_dir: Path) -> list[str]:
    paths: list[str] = []

    for node in model.test_nodes:
        capture_dirs = find_prometheus_capture_dirs(node)
        if not capture_dirs:
            continue

        node_name = Path(node.directory).name
        node_output_dir = output_dir / node_name

        for capture_dir in capture_dirs:
            try:
                group_result = parse_capture_group(capture_dir)
                report_path = generate_group_report(group_result, node_output_dir)
                paths.append(str(report_path))
                logger.info("Generated Prometheus report: %s", report_path)
            except Exception:
                logger.warning(
                    "Failed to generate Prometheus report for %s",
                    capture_dir,
                    exc_info=True,
                )

    return paths


def find_prometheus_capture_dirs(test_node: BaseTestNode) -> list[Path]:
    seen = set()
    results = []

    for artifact_path in test_node.artifact_paths:
        path = Path(artifact_path)
        if path.name != "metrics_metadata.yaml":
            continue
        if path.parent.name != "prometheus_metrics":
            continue

        capture_dir = path.parent.parent
        profiles_dir = capture_dir / "src" / "metric_profiles"
        if not profiles_dir.is_dir():
            continue

        if capture_dir not in seen:
            seen.add(capture_dir)
            results.append(capture_dir)

    return sorted(results)


def get_plugin() -> AgPraxisPlugin:
    return AgPraxisPlugin()
