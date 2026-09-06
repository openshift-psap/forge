"""GuideLLM post-processing with the llm-d dashboard CSV schema."""

from __future__ import annotations

import re
from typing import Any

import yaml

from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.kpi.analyze import AnalysisConfig
from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)
from projects.guidellm.postprocess.guidellm.dashboard import (
    deployment_metadata_from_profile,
    enrich_guidellm_parse_result,
)
from projects.guidellm.postprocess.guidellm.plugin import GuideLLMPlugin
from projects.llm_d.orchestration.render_inference_service import _build_vllm_args
from projects.llm_d.orchestration.runtime_config import deep_merge

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


class LlmDGuideLLMPlugin(GuideLLMPlugin):
    """Keep generic GuideLLM outputs and add the llm-d dashboard projection."""

    def __init__(self):
        super().__init__()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
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

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute KPI values using dataclasses with status details."""
        # Store model for independent dashboard KPI generation in CSV export
        self._cached_model = model
        return super().compute_kpis(model)


def get_plugin() -> PostProcessingPlugin:
    return LlmDGuideLLMPlugin()


def _extract_deployment_metadata(node: TestBaseNode) -> dict[str, Any]:
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


def _extract_serving_image(node: TestBaseNode) -> str | None:
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


def _extract_accelerator(node: TestBaseNode) -> str | None:
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
