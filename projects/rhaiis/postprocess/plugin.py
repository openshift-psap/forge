from __future__ import annotations

from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi import KpiComputationStatus, KpiRecord
from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)

from .kpis import RhaiisKpiHandler
from .parser import RhaiisParser


def _prefix_caching_from_runtime_args(runtime_args: str) -> str:
    """Return "yes"/"no" from the enable/no-enable-prefix-caching flag in
    runtime_args, or "" if neither is set. If both appear, the last one
    wins (matches vLLM/argparse's BooleanOptionalAction behavior).
    """
    result = ""
    for part in runtime_args.split(";"):
        key, sep, value = part.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().lower()
        if key == "no-enable-prefix-caching":
            result = "no" if value == "true" else "yes"
        elif key == "enable-prefix-caching":
            result = "yes" if value == "true" else "no"
    return result


class RhaiisPlugin(PostProcessingPlugin):
    def __init__(self) -> None:
        self.parser = RhaiisParser()
        self.kpi_handler = RhaiisKpiHandler()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        parsed = self.parser.parse(nodes)
        nodes_by_path = {str(node.test_path): node for node in nodes}
        for record in parsed.records:
            node = nodes_by_path.get(record.test_base_path)
            mlflow_dest = node.test_labels.get("mlflow_destination", {}) if node else {}
            if mlflow_dest:
                record.metrics.setdefault("mlflow_run_id", mlflow_dest.get("run_id", ""))
                record.metrics.setdefault(
                    "mlflow_experiment_id", mlflow_dest.get("experiment_id", "")
                )
        return parsed

    def get_available_reports(self) -> dict[str, dict[str, str]]:
        return {}

    def get_available_reports_by_type(self) -> dict[str, dict[str, str]]:
        return {"reports": {}, "plots": {}}

    def get_reports_only(self) -> dict[str, str]:
        return {}

    def get_plots_only(self) -> dict[str, str]:
        return {}

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[str]:
        return []

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute KPIs using dataclasses with status details."""
        return self.kpi_handler.compute_kpis(model)

    def export_dashboard_csv(self, model: UnifiedRunModel, output_path: Path) -> str:
        """Generate dashboard CSV using shared architecture."""
        from projects.guidellm.postprocess.guidellm.dashboard import DashboardCsvExporter
        from projects.rhaiis.postprocess.csv_dashboard import RHAIIS_FIELDNAMES

        def metadata_row_mapper(labels: dict[str, Any]) -> dict[str, Any]:
            """Extract RHAIIS metadata for CSV row from dashboard KPI labels."""
            acc = labels.get("accelerator", "").upper()
            cluster_tag = labels.get("cluster_tag", "")
            model_id = labels.get("hf_model_id", "")
            tp = labels.get("tensor_parallel_size", "1")
            run_name = (
                f"{acc}-{cluster_tag}-{model_id}-{tp}" if cluster_tag else f"{acc}-{model_id}-{tp}"
            )
            return {
                "run": run_name,
                "accelerator": acc,
                "model": model_id,
                "version": labels.get("version", ""),
                "TP": tp,
                "prompt toks": labels.get("prompt_toks", ""),
                "output toks": labels.get("output_toks", ""),
                "image_tag": labels.get("image_tag", ""),
                "runtime_args": labels.get("runtime_args", ""),
                "uuid": labels.get("run_uuid", ""),
                "guidellm_start_time_ms": labels.get("guidellm_start_time_ms", ""),
                "guidellm_end_time_ms": labels.get("guidellm_end_time_ms", ""),
                "guidellm_version": labels.get("guidellm_version", ""),
                "mlflow_run_id": labels.get("mlflow_run_id", ""),
                "mlflow_experiment_id": labels.get("mlflow_experiment_id", ""),
                "turns": labels.get("turns", ""),
                "prefix_tokens": labels.get("prefix_tokens", ""),
                "prefix_count": labels.get("prefix_count", ""),
                "request_type": labels.get("request_type", ""),
                "prefix_caching": _prefix_caching_from_runtime_args(labels.get("runtime_args", "")),
                "DP": labels.get("DP", ""),
                "dataset": labels.get("dataset", ""),
                "spec_decoding": labels.get("spec_decoding", ""),
            }

        exporter = DashboardCsvExporter()
        return exporter.export_dashboard_csv(
            model,
            output_path,
            prefix="rhaiis",
            fieldnames=RHAIIS_FIELDNAMES,
            metadata_row_mapper=metadata_row_mapper,
        )

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, Any]:
        return {}


def get_plugin() -> PostProcessingPlugin:
    return RhaiisPlugin()
