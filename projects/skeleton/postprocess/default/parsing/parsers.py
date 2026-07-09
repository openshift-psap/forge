"""File parsers for Skeleton Caliper plugin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from projects.caliper.engine.model import (
    ParseResult,
    TestBaseNode,
    UnifiedResultRecord,
)


def _labels_from_node(node: TestBaseNode) -> dict[str, Any]:
    """Extract labels from a test node."""
    raw = node.labels
    inner = raw.get("labels")
    if isinstance(inner, dict):
        return dict(inner)
    if isinstance(raw, dict):
        return dict(raw)
    return {"facet": "default"}


class SkeletonParser:
    """Parser for skeleton project test artifacts."""

    def parse_metrics_json(self, file_path: Path) -> tuple[dict[str, Any], list[str]]:
        """
        Parse a metrics.json file.

        Returns:
            Tuple of (metrics dict, warnings list)
        """
        warnings: list[str] = []

        try:
            metrics = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(metrics, dict):
                warnings.append(f"{file_path}: metrics.json must be a JSON object")
                return {}, warnings
            return metrics, warnings
        except json.JSONDecodeError as e:
            warnings.append(f"Malformed JSON {file_path}: {e}")
            return {"_parse_error": True}, warnings

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        """
        Parse test nodes containing metrics.json files.

        Args:
            nodes: List of test nodes to parse

        Returns:
            ParseResult with unified records and warnings
        """
        records: list[UnifiedResultRecord] = []
        warnings: list[str] = []

        for node in nodes:
            metrics: dict[str, Any] = {}
            for p in node.artifact_paths:
                if p.name != "metrics.json":
                    continue

                node_metrics, node_warnings = self.parse_metrics_json(p)
                metrics = node_metrics
                warnings.extend(node_warnings)
                break

            labels = _labels_from_node(node)
            records.append(
                UnifiedResultRecord(
                    test_base_path=str(node.test_path),
                    distinguishing_labels=labels,
                    metrics=dict(metrics) if metrics else {"throughput": 0.0},
                    run_identity={"skeleton_sample": True},
                    parse_notes=[],
                )
            )

        return ParseResult(records=records, warnings=warnings)
