"""Stub post-processing plugin for tests and demos."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedResultRecord,
    UnifiedRunModel,
)


class StubPlugin(PostProcessingPlugin):
    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        records: list[UnifiedResultRecord] = []
        warnings: list[str] = []
        for node in nodes:
            labels = (
                node.labels.get("labels")
                if isinstance(node.labels.get("labels"), dict)
                else node.labels
            )
            raw = {}
            for p in node.artifact_paths:
                if p.name == "metrics.json":
                    import json  # noqa: PLC0415

                    try:
                        raw = json.loads(p.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as e:
                        warnings.append(f"Malformed JSON {p}: {e}")
                        raw = {"_error": "partial_parse"}
            dist = labels if isinstance(labels, dict) else {}
            records.append(
                UnifiedResultRecord(
                    test_base_path=str(node.test_path),
                    distinguishing_labels=dict(dist) if dist else {"facet": "default"},
                    metrics=raw or {"throughput": 1.0},
                    run_identity={"stub": True},
                    parse_notes=[],
                )
            )
        return ParseResult(records=records, warnings=warnings)

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, object] | None,
    ) -> list[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / "report.html"
        rid = ",".join(report_ids or []) or "default"
        html_path.write_text(
            f"<html><body><h1>Stub report</h1><p>{rid}</p></body></html>",
            encoding="utf-8",
        )
        return [str(html_path)]

    def kpi_catalog(self) -> list[dict[str, object]]:
        return [
            {
                "kpi_id": "throughput_rps",
                "name": "Throughput",
                "unit": "req/s",
                "higher_is_better": True,
            }
        ]

    def compute_kpis(self, model: UnifiedRunModel) -> list[dict[str, object]]:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict[str, object]] = []
        for r in model.unified_result_records:
            m = r.metrics.get("throughput", 0.0)
            try:
                val = float(m)
            except (TypeError, ValueError):
                val = None
            out.append(
                {
                    "schema_version": "1",
                    "kpi_id": "throughput_rps",
                    "value": val,
                    "unit": "req/s",
                    "run_id": r.test_base_path,
                    "timestamp": ts,
                    "labels": {
                        **r.distinguishing_labels,
                        "higher_is_better": True,
                    },
                    "source": {
                        "test_base_path": r.test_base_path,
                        "plugin_module": model.plugin_module,
                    },
                }
            )
        return out

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, object]:
        return {
            "schema_version": "1",
            "run_id": model.base_directory,
            "metrics": {"records": len(model.unified_result_records)},
            "optional": {},
        }


def get_plugin() -> PostProcessingPlugin:
    return StubPlugin()
