"""GuideLLM post-processing with the llm-d dashboard CSV schema."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.kpi.analyze import AnalysisConfig
from projects.caliper.engine.model import (
    BaseTestNode,
    ParseResult,
    PostProcessingPlugin,
    ReportFile,
    UnifiedRunModel,
)
from projects.caliper.postprocess.prometheus.parser import parse_capture_group
from projects.caliper.postprocess.prometheus.report import generate_group_report
from projects.guidellm.postprocess.guidellm.dashboard import (
    deployment_metadata_from_profile,
    enrich_guidellm_parse_result,
)
from projects.guidellm.postprocess.guidellm.kpi_handler import GuideLLMKpiHandler
from projects.guidellm.postprocess.guidellm.plugin import PLOT_REGISTRY, GuideLLMPlugin
from projects.llm_d.orchestration.render_inference_service import _build_vllm_args
from projects.llm_d.orchestration.runtime_config import deep_merge

logger = logging.getLogger(__name__)

# Analysis configuration for KPI regression testing
analysis_config = AnalysisConfig(
    comparison_labels=["product_version"],
    ignored_labels=["cluster"],
    sorting_labels=["product_version"],
    regression_config={
        "SCALAR_RELATIVE_CHANGE": {
            "max_relative_regression": 0.15,  # 15% threshold (LLM performance can vary more)
            "min_baseline_points": 2,  # Require at least 2 baseline points for reliability
        },
        "CURVE_AUC_CHANGE": {
            "max_relative_regression": 0.35,  # 35% threshold (LLM performance can vary more)
            "min_baseline_points": 2,  # Require at least 2 baseline points for reliability
        },
    },
)


LLM_D_REQUIRED_LABELS = GuideLLMKpiHandler.REQUIRED_LABELS | {
    "product_version",
    "benchmark_key",
    "deployment_profile",
    "gpu_type",
    "platform",
    "test_harness",
}

PROMETHEUS_REPORT_ID = "report_prometheus_metrics"


class LlmDGuideLLMPlugin(GuideLLMPlugin):
    """Keep generic GuideLLM outputs and add the llm-d dashboard projection."""

    def __init__(self):
        super().__init__()
        self.kpi_handler = GuideLLMKpiHandler(
            required_labels=LLM_D_REQUIRED_LABELS,
        )
        PLOT_REGISTRY[PROMETHEUS_REPORT_ID] = {
            "function": None,
            "type": "report",
            "description": "Prometheus metrics time-series reports from capture artifacts",
        }

    def parse(self, nodes: list[BaseTestNode]) -> ParseResult:
        parsed = enrich_guidellm_parse_result(super().parse(nodes), nodes)
        nodes_by_path = {str(node.test_path): node for node in nodes}
        records = []
        for record in parsed.records:
            node = nodes_by_path.get(record.test_base_path)
            deployment_metadata = _extract_deployment_metadata(node) if node else {}
            test_labels = node.test_labels.get("labels", {}) if node else {}
            hf_model_id = test_labels.get("model_name")
            if hf_model_id:
                record.metrics["hf_model_id"] = hf_model_id
            mlflow_dest = node.test_labels.get("mlflow_destination", {}) if node else {}
            if mlflow_dest:
                record.metrics.setdefault("mlflow_run_id", mlflow_dest.get("run_id", ""))
                record.metrics.setdefault(
                    "mlflow_experiment_id", mlflow_dest.get("experiment_id", "")
                )
            for key, value in deployment_metadata.items():
                record.metrics.setdefault(key, value)
            records.append(record)
        return ParseResult(records=records, warnings=parsed.warnings)

    def kpi_catalog(self) -> list[KpiCatalogEntry]:
        return self.kpi_handler.get_catalog()

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[ReportFile]:
        wanted = list(dict.fromkeys(report_ids or ()))
        prom_requested = PROMETHEUS_REPORT_ID in wanted
        parent_ids = [r for r in wanted if r != PROMETHEUS_REPORT_ID] or None

        reports = super().visualize(model, output_dir, parent_ids, group_id, visualize_config)

        if prom_requested:
            prom_reports = _generate_prometheus_reports(model, output_dir)
            reports.extend(prom_reports)
            if not prom_reports:
                logger.info("No Prometheus capture data found in test artifacts")

        return reports

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute KPI values using dataclasses with status details."""
        # Store model for independent dashboard KPI generation in CSV export
        self._cached_model = model
        return super().compute_kpis(model)

    def export_dashboard_csv(self, model: UnifiedRunModel, output_path: Path) -> str:
        """Generate dashboard CSV using shared architecture with LLM-D specific metadata mapping."""
        from projects.guidellm.postprocess.guidellm.dashboard import (
            DashboardCsvExporter,
            normalize_product_version,
        )

        from .csv_dashboard import DASHBOARD_FIELDNAMES

        def metadata_row_mapper(labels: dict[str, Any]) -> dict[str, Any]:
            """Extract LLM-D specific metadata for CSV row from dashboard KPI labels."""
            accelerator = labels.get("gpu_type") or labels.get("accelerator", "")
            model_id = labels.get("hf_model_id") or labels.get("model_name", "")
            run_model = model_id.replace("/", "-")
            tp = labels.get("tensor_parallel_size", "")
            replicas = labels.get("replicas", "")

            # LLM-D specific version handling
            version = normalize_product_version(
                labels.get("product_version") or labels.get("version", "")
            )
            deployment_profile = labels.get("deployment_profile", "")
            if version and deployment_profile:
                version = f"{version}-{deployment_profile}"

            return {
                "run": "-".join(str(value) for value in (accelerator, run_model, tp) if value),
                "accelerator": accelerator,
                "model": model_id,
                "version": version,
                "prompt toks": labels.get("prompt_toks", ""),
                "output toks": labels.get("output_toks", ""),
                "TP": tp,
                "DP": labels.get("DP") or labels.get("data_parallel_size") or "",
                "EP": labels.get("EP") or labels.get("expert_parallel_size") or "",
                "replicas": replicas,
                "prefill_pod_count": labels.get("prefill_pod_count", ""),
                "decode_pod_count": labels.get("decode_pod_count", ""),
                "router_config": labels.get("router_config", ""),
                "uuid": labels.get("run_uuid", ""),
                "runtime_args": labels.get("runtime_args", ""),
                "guidellm_start_time_ms": labels.get("guidellm_start_time_ms", ""),
                "guidellm_end_time_ms": labels.get("guidellm_end_time_ms", ""),
                "image_tag": labels.get("image_tag", ""),
                "guidellm_version": labels.get("guidellm_version", ""),
                "mlflow_run_id": labels.get("mlflow_run_id", ""),
                "mlflow_experiment_id": labels.get("mlflow_experiment_id", ""),
                "turns": labels.get("turns", ""),
                "turn_index": labels.get("turn", ""),
                "notes": labels.get("notes", ""),
            }

        exporter = DashboardCsvExporter()
        return exporter.export_dashboard_csv(
            model,
            output_path,
            prefix="llmd",
            fieldnames=DASHBOARD_FIELDNAMES,
            metadata_row_mapper=metadata_row_mapper,
        )


def get_plugin() -> PostProcessingPlugin:
    return LlmDGuideLLMPlugin()


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


def _extract_deployment_metadata(node: BaseTestNode) -> dict[str, Any]:
    """Recover llm-d deployment metadata when only config.yaml was exported."""
    config_path = next((path for path in node.artifact_paths if path.name == "config.yaml"), None)
    if config_path is None:
        return {}
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(config, dict):
        return {}

    runtime = config.get("runtime", {})
    deployments = config.get("deployments", {})
    profile_name = runtime.get("deployment_profile")
    defaults = deployments.get("defaults", {})
    profile_override = deployments.get("profiles", {}).get(profile_name, {})
    profile = deep_merge(defaults, profile_override)

    metadata = {
        "model_name": runtime.get("model_name"),
        "hf_model_id": runtime.get("model_name"),
        "replicas": profile.get("replicas"),
        "tensor_parallel_size": profile.get("tensor_parallelism"),
    }
    configured_labels = config.get("cpt", {}).get("kpi", {}).get("labels", {})
    metadata["gpu_type"] = configured_labels.get("gpu_type") or _extract_accelerator(node)
    metadata["product_version"] = configured_labels.get("product_version")
    metadata.update(deployment_metadata_from_profile(profile, profile_name=profile_name))
    vllm_args = profile.get("vllm_extra", {}).get("args", {})
    if vllm_args:
        metadata["runtime_args"] = " ".join(_build_vllm_args(vllm_args))
    serving_image = profile.get("serving_image")
    if not serving_image:
        serving_image = _extract_serving_image(node)
    if serving_image:
        metadata["image_tag"] = serving_image
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def _extract_serving_image(node: BaseTestNode) -> str | None:
    deployment_path = next(
        (
            path
            for path in node.artifact_paths
            if path.name
            in {
                "llminferenceservice.deployments.json",
                "llminferenceservice.deployments.yaml",
            }
        ),
        None,
    )
    if deployment_path is None:
        return None
    try:
        deployments = yaml.safe_load(deployment_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    for deployment in deployments.get("items", []) if isinstance(deployments, dict) else []:
        containers = (
            deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
        for container in containers:
            if container.get("name") == "main" and container.get("image"):
                return str(container["image"])
    return None


def _extract_accelerator(node: BaseTestNode) -> str | None:
    """Infer the GPU family from captured serving-pod placement."""
    pods_path = next(
        (path for path in node.artifact_paths if path.name == "llminferenceservice.pods.yaml"),
        None,
    )
    if pods_path is None:
        return None
    try:
        pods = yaml.safe_load(pods_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    for pod in pods.get("items", []) if isinstance(pods, dict) else []:
        node_name = str(pod.get("spec", {}).get("nodeName", ""))
        match = re.search(r"(?:^|-)gpu-([a-z]+\d+[a-z0-9]*)(?:-|$)", node_name, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None
