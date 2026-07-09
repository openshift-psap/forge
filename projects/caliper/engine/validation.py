"""jsonschema validation helpers (FR-012)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from projects.caliper.engine.model import UnifiedRunModel


def load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_instance(instance: Any, schema: dict[str, Any], what: str) -> None:
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as e:
        raise ValueError(f"{what} validation failed: {e.message}") from e


def schema_path(package_relative: str) -> Path:
    """Path under schemas/ next to this package."""
    here = Path(__file__).resolve().parent.parent
    return here / "schemas" / package_relative


def model_to_jsonable(model: UnifiedRunModel) -> dict[str, Any]:
    """Serialize unified model for cache (simplified)."""

    def node_to_dict(n: Any) -> dict[str, Any]:
        return {
            "directory": str(n.directory),
            "labels": n.labels,
            "artifact_paths": [str(p) for p in n.artifact_paths],
            "test_path": str(n.test_path),
        }

    def rec_to_dict(r: Any) -> dict[str, Any]:
        return {
            "test_base_path": r.test_base_path,
            "distinguishing_labels": r.distinguishing_labels,
            "metrics": r.metrics,
            "run_identity": r.run_identity,
            "parse_notes": r.parse_notes,
        }

    return {
        "plugin_module": model.plugin_module,
        "base_directory": model.base_directory,
        "test_nodes": [node_to_dict(n) for n in model.test_nodes],
        "unified_result_records": [rec_to_dict(r) for r in model.unified_result_records],
        "schema_version": model.schema_version,
    }


def model_from_jsonable(data: dict[str, Any]) -> UnifiedRunModel:
    from projects.caliper.engine.model import (  # noqa: PLC0415
        TestBaseNode,
        UnifiedResultRecord,
    )

    base_dir = Path(data["base_directory"])
    nodes = [
        TestBaseNode(
            directory=Path(n["directory"]),
            labels=n["labels"],
            artifact_paths=[Path(p) for p in n.get("artifact_paths", [])],
            test_path=Path(n.get("test_path", str(Path(n["directory"]).relative_to(base_dir)))),
        )
        for n in data["test_nodes"]
    ]
    records = [
        UnifiedResultRecord(
            test_base_path=r["test_base_path"],
            distinguishing_labels=r["distinguishing_labels"],
            metrics=r["metrics"],
            run_identity=r.get("run_identity", {}),
            parse_notes=r.get("parse_notes", []),
        )
        for r in data["unified_result_records"]
    ]
    return UnifiedRunModel(
        plugin_module=data["plugin_module"],
        base_directory=data["base_directory"],
        test_nodes=nodes,
        unified_result_records=records,
        schema_version=data.get("schema_version", "1"),
    )
