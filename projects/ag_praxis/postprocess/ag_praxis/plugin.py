from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import BaseTestNode, ReportFile, UnifiedRunModel
from projects.caliper.postprocess.prometheus.parser import parse_capture_group
from projects.caliper.postprocess.prometheus.report import generate_group_report
from projects.guidellm.postprocess.guidellm.plugin import PLOT_REGISTRY, GuideLLMPlugin

logger = logging.getLogger(__name__)

PROMETHEUS_REPORT_ID = "report_prometheus_metrics"
RESOURCE_REPORT_ID = "report_payload_resources"
OVERHEAD_REPORT_ID = "report_overhead_analysis"


class AgPraxisPlugin(GuideLLMPlugin):
    def __init__(self):
        super().__init__()
        PLOT_REGISTRY[PROMETHEUS_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "Prometheus metrics time-series reports from capture artifacts",
        }
        PLOT_REGISTRY[RESOURCE_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "CPU/memory usage vs limits for payload pods",
        }
        PLOT_REGISTRY[OVERHEAD_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "Endpoint overhead analysis comparing svc, maas, and maas/praxis",
        }

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[ReportFile]:
        wanted = list(dict.fromkeys(report_ids or ()))
        our_ids = {PROMETHEUS_REPORT_ID, RESOURCE_REPORT_ID, OVERHEAD_REPORT_ID}
        prom_requested = PROMETHEUS_REPORT_ID in wanted
        resource_requested = RESOURCE_REPORT_ID in wanted
        overhead_requested = OVERHEAD_REPORT_ID in wanted
        parent_ids = [r for r in wanted if r not in our_ids] or None

        reports = super().visualize(model, output_dir, parent_ids, group_id, visualize_config)

        if prom_requested:
            prom_reports = _generate_prometheus_reports(model, output_dir)
            reports.extend(prom_reports)
            if not prom_reports:
                logger.info("No Prometheus capture data found in test artifacts")

        if resource_requested:
            resource_reports = _generate_resource_reports(model, output_dir)
            reports.extend(resource_reports)
            if not resource_reports:
                logger.info("No resource_payload capture data found in test artifacts")

        if overhead_requested:
            overhead_reports = _generate_overhead_reports(model, output_dir)
            reports.extend(overhead_reports)
            if not overhead_reports:
                logger.info("No overhead analysis data found in test artifacts")

        return reports


def _generate_overhead_reports(model: UnifiedRunModel, output_dir: Path) -> list[ReportFile]:
    from projects.ag_praxis.postprocess.ag_praxis.overhead_report import (
        generate_overhead_report,
    )

    guidellm_records = [
        r
        for r in model.unified_result_records
        if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
    ]

    source_dir = output_dir / "global" / "overhead_analysis"
    try:
        report_path = generate_overhead_report(
            guidellm_records, source_dir, base_directory=model.base_directory
        )
        if report_path:
            logger.info("Generated overhead report: %s", report_path)
            rel_path = str(Path(report_path).relative_to(output_dir))
            return [
                ReportFile(
                    source="overhead_analysis",
                    scope="global",
                    title="Overhead Analysis",
                    path=rel_path,
                )
            ]
    except Exception:
        logger.warning("Failed to generate overhead report", exc_info=True)

    return []


def _generate_resource_reports(model: UnifiedRunModel, output_dir: Path) -> list[ReportFile]:
    from projects.ag_praxis.postprocess.ag_praxis.resource_report import (
        generate_resource_report,
    )

    reports: list[ReportFile] = []

    for node in model.test_nodes:
        capture_dirs = find_prometheus_capture_dirs(node)
        if not capture_dirs:
            continue

        node_name = Path(node.directory).name
        source_dir = output_dir / node_name / "resource_usage_vs_limits"

        for capture_dir in capture_dirs:
            try:
                group_result = parse_capture_group(capture_dir)
                if "payload" not in group_result.group_name:
                    continue
                report_path = generate_resource_report(group_result, source_dir)
                if report_path:
                    rel_path = str(Path(report_path).relative_to(output_dir))
                    reports.append(
                        ReportFile(
                            source="resource_usage_vs_limits",
                            scope=node_name,
                            title=group_result.group_name,
                            path=rel_path,
                        )
                    )
                    logger.info("Generated resource report: %s", report_path)
            except Exception:
                logger.warning(
                    "Failed to generate resource report for %s",
                    capture_dir,
                    exc_info=True,
                )

    return reports


def _generate_prometheus_reports(model: UnifiedRunModel, output_dir: Path) -> list[ReportFile]:
    reports: list[ReportFile] = []

    for node in model.test_nodes:
        capture_dirs = find_prometheus_capture_dirs(node)
        if not capture_dirs:
            continue

        node_name = Path(node.directory).name
        source_dir = output_dir / node_name / "prometheus_metrics"

        for capture_dir in capture_dirs:
            try:
                group_result = parse_capture_group(capture_dir)
                report_path = generate_group_report(group_result, source_dir)
                rel_path = str(Path(report_path).relative_to(output_dir))
                reports.append(
                    ReportFile(
                        source="prometheus_metrics",
                        scope=node_name,
                        title=group_result.group_name,
                        path=rel_path,
                    )
                )
                logger.info("Generated Prometheus report: %s", report_path)
            except Exception:
                logger.warning(
                    "Failed to generate Prometheus report for %s",
                    capture_dir,
                    exc_info=True,
                )

    return reports


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
