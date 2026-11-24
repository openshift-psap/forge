from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import (
    BaseTestNode,
    ReportFile,
    UnifiedResultRecord,
    UnifiedRunModel,
)
from projects.caliper.postprocess.prometheus.parser import parse_capture_group
from projects.caliper.postprocess.prometheus.report import generate_group_report
from projects.guidellm.postprocess.guidellm.plugin import PLOT_REGISTRY, GuideLLMPlugin

logger = logging.getLogger(__name__)

PROMETHEUS_REPORT_ID = "report_prometheus_metrics"
RESOURCE_REPORT_ID = "report_payload_resources"
OVERHEAD_REPORT_ID = "report_overhead_analysis"
AUTH_SATURATION_REPORT_ID = "report_auth_saturation"
CLIFF_REPORT_ID = "report_500_cliff"


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
            "description": "CPU/memory usage vs limits and requests for every captured target",
        }
        PLOT_REGISTRY[OVERHEAD_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "Endpoint overhead analysis comparing svc, maas/ipp, and maas/praxis",
        }
        PLOT_REGISTRY[AUTH_SATURATION_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "Authorino auth-path saturation: gateway 200/500 rates, authorino CPU plateau, 500 deadline pinning, and Kubernetes review rates on one timeline",
        }
        PLOT_REGISTRY[CLIFF_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "Per-test 500-cliff analysis: observed ext_proc budget and identity metrics (from captured cluster state) against gateway 500 rates, the Envoy-local failure split and ext_proc occupancy",
        }

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
        plugin_settings: dict[str, Any] | None = None,
    ) -> list[ReportFile]:
        wanted = list(dict.fromkeys(report_ids or ()))
        our_ids = {
            PROMETHEUS_REPORT_ID,
            RESOURCE_REPORT_ID,
            OVERHEAD_REPORT_ID,
            AUTH_SATURATION_REPORT_ID,
            CLIFF_REPORT_ID,
        }
        prom_requested = PROMETHEUS_REPORT_ID in wanted
        resource_requested = RESOURCE_REPORT_ID in wanted
        overhead_requested = OVERHEAD_REPORT_ID in wanted
        saturation_requested = AUTH_SATURATION_REPORT_ID in wanted
        cliff_requested = CLIFF_REPORT_ID in wanted
        parent_ids = [r for r in wanted if r not in our_ids] or None

        reports = super().visualize(
            model,
            output_dir,
            parent_ids,
            group_id,
            visualize_config,
            plugin_settings=plugin_settings,
        )

        if prom_requested:
            prom_reports = _generate_prometheus_reports(model, output_dir)
            reports.extend(prom_reports)
            if not prom_reports:
                logger.info("No Prometheus capture data found in test artifacts")

        if resource_requested:
            resource_reports = _generate_resource_reports(model, output_dir)
            reports.extend(resource_reports)
            if not resource_reports:
                logger.info("No resource usage/limits capture data found in test artifacts")

        if saturation_requested:
            saturation_reports = _generate_auth_saturation_reports(model, output_dir)
            reports.extend(saturation_reports)
            if not saturation_reports:
                logger.info("No auth saturation capture data found in test artifacts")

        if cliff_requested:
            cliff_reports = _generate_500_cliff_reports(model, output_dir)
            reports.extend(cliff_reports)
            if not cliff_reports:
                logger.info("No 500-cliff data found in test artifacts")

        if overhead_requested:
            overhead_reports = _generate_overhead_reports(model, output_dir)
            reports.extend(overhead_reports)
            if not overhead_reports:
                logger.info("No overhead analysis data found in test artifacts")

        return reports


def _generate_auth_saturation_reports(model: UnifiedRunModel, output_dir: Path) -> list[ReportFile]:
    from projects.ag_praxis.postprocess.ag_praxis.auth_saturation_report import (
        generate_auth_saturation_report,
    )

    reports: list[ReportFile] = []

    for node in model.test_nodes:
        capture_dirs = find_prometheus_capture_dirs(node)
        if not capture_dirs:
            continue

        node_scope = str(node.test_path)
        source_dir = output_dir / node.test_path / "auth_saturation"

        group_results = []
        for capture_dir in capture_dirs:
            try:
                group_results.append(parse_capture_group(capture_dir))
            except Exception:
                logger.warning("Failed to parse Prometheus capture %s", capture_dir, exc_info=True)

        if not group_results:
            continue

        try:
            report_path = generate_auth_saturation_report(group_results, source_dir)
        except Exception:
            logger.warning(
                "Failed to generate auth saturation report for %s", node_scope, exc_info=True
            )
            continue

        if report_path:
            logger.info("Generated auth saturation report: %s", report_path)
            rel_path = str(Path(report_path).relative_to(output_dir))
            reports.append(
                ReportFile(
                    source="auth_saturation",
                    scope=node_scope,
                    title="Auth Path Saturation",
                    path=rel_path,
                )
            )

    return reports


def _generate_500_cliff_reports(model: UnifiedRunModel, output_dir: Path) -> list[ReportFile]:
    from projects.ag_praxis.postprocess.ag_praxis.cliff_report import generate_500_cliff_report
    from projects.ag_praxis.postprocess.ag_praxis.k8s_state import load_praxis_k8s_state

    reports: list[ReportFile] = []

    for node in model.test_nodes:
        k8s_state = load_praxis_k8s_state(node)
        capture_dirs = find_prometheus_capture_dirs(node)
        if k8s_state is None and not capture_dirs:
            continue

        node_scope = str(node.test_path)
        source_dir = output_dir / node.test_path / "500_cliff"

        group_results = []
        for capture_dir in capture_dirs:
            try:
                group_results.append(parse_capture_group(capture_dir))
            except Exception:
                logger.warning("Failed to parse Prometheus capture %s", capture_dir, exc_info=True)

        try:
            report_path = generate_500_cliff_report(group_results, k8s_state, source_dir)
        except Exception:
            logger.warning("Failed to generate 500-cliff report for %s", node_scope, exc_info=True)
            continue

        if report_path:
            logger.info("Generated 500-cliff report: %s", report_path)
            rel_path = str(Path(report_path).relative_to(output_dir))
            reports.append(
                ReportFile(
                    source="500_cliff",
                    scope=node_scope,
                    title="Gateway 500 Cliff",
                    path=rel_path,
                )
            )

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

    # One report per test: endpoint nodes (svc, maas_ipp, maas_praxis, ...) are siblings
    # under their matrix-combination directory, so group records by their parent directory.
    records_by_test: dict[str, list[UnifiedResultRecord]] = {}
    for record in guidellm_records:
        if not record.distinguishing_labels.get("endpoint"):
            continue
        test_scope = str(Path(record.test_base_path).parent)
        records_by_test.setdefault(test_scope, []).append(record)

    reports: list[ReportFile] = []
    for test_scope, test_records in sorted(records_by_test.items()):
        source_dir = output_dir / test_scope / "overhead_analysis"
        try:
            report_path = generate_overhead_report(
                test_records, source_dir, base_directory=model.base_directory
            )
        except Exception:
            logger.warning("Failed to generate overhead report for %s", test_scope, exc_info=True)
            continue

        if report_path:
            logger.info("Generated overhead report: %s", report_path)
            rel_path = str(Path(report_path).relative_to(output_dir))
            reports.append(
                ReportFile(
                    source="overhead_analysis",
                    scope=test_scope,
                    title="Overhead Analysis",
                    path=rel_path,
                )
            )

    return reports


def _generate_resource_reports(model: UnifiedRunModel, output_dir: Path) -> list[ReportFile]:
    from projects.ag_praxis.postprocess.ag_praxis.resource_report import (
        generate_resource_report,
    )

    reports: list[ReportFile] = []

    for node in model.test_nodes:
        capture_dirs = find_prometheus_capture_dirs(node)
        if not capture_dirs:
            continue

        node_scope = str(node.test_path)
        source_dir = output_dir / node.test_path / "resource_usage_vs_limits"

        group_results = []
        for capture_dir in capture_dirs:
            try:
                group_results.append(parse_capture_group(capture_dir))
            except Exception:
                logger.warning("Failed to parse Prometheus capture %s", capture_dir, exc_info=True)

        if not group_results:
            continue

        try:
            report_path = generate_resource_report(group_results, source_dir)
        except Exception:
            logger.warning("Failed to generate resource report for %s", node_scope, exc_info=True)
            continue

        if report_path:
            rel_path = str(Path(report_path).relative_to(output_dir))
            reports.append(
                ReportFile(
                    source="resource_usage_vs_limits",
                    scope=node_scope,
                    title="Resource Usage vs Limits",
                    path=rel_path,
                )
            )
            logger.info("Generated resource report: %s", report_path)

    return reports


def _generate_prometheus_reports(model: UnifiedRunModel, output_dir: Path) -> list[ReportFile]:
    reports: list[ReportFile] = []

    for node in model.test_nodes:
        capture_dirs = find_prometheus_capture_dirs(node)
        if not capture_dirs:
            continue

        node_scope = str(node.test_path)
        source_dir = output_dir / node.test_path / "prometheus_metrics"

        for capture_dir in capture_dirs:
            try:
                group_result = parse_capture_group(capture_dir)
                report_path = generate_group_report(group_result, source_dir)
                rel_path = str(Path(report_path).relative_to(output_dir))
                reports.append(
                    ReportFile(
                        source="prometheus_metrics",
                        scope=node_scope,
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
