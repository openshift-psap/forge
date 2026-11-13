"""Persistent test-directory → child-run mapping for resumable export.

Multi-run export creates one nested MLflow child run per Caliper test node. A
repeated export must resume those children instead of creating new ones every
time. This module persists the mapping at the job artifact root (alongside the
existing ``_mlflow_destination.yaml`` destination marker — not a competing
marker) and resolves stable keys from test-dir identity.

The mapping is scoped to one export destination (tracking URI, experiment, and
parent run ID). A resume against a *different* destination raises rather than
attaching stale children to the wrong parent. Display names alone are never
used as identity: two test nodes can render the same name, but a relative test
directory cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CHILD_RUN_MAP_FILENAME = "_mlflow_child_runs.yaml"
CHILD_RUN_MAP_VERSION = 1


class ChildRunMapError(RuntimeError):
    """Raised when a persisted child-run map cannot be safely reused."""


@dataclass(frozen=True)
class ChildRunDestination:
    """The export destination a child-run map is scoped to."""

    tracking_uri: str
    experiment_id: str
    parent_run_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "tracking_uri": self.tracking_uri,
            "experiment_id": self.experiment_id,
            "parent_run_id": self.parent_run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChildRunDestination:
        return cls(
            tracking_uri=str(data.get("tracking_uri", "")),
            experiment_id=str(data.get("experiment_id", "")),
            parent_run_id=str(data.get("parent_run_id", "")),
        )


def child_run_key(test_dir: Path, artifact_root: Path) -> str:
    """Return the stable key for a test directory (its POSIX relative path)."""
    resolved = test_dir.resolve()
    root = artifact_root.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"test directory {test_dir} is not under artifact root {artifact_root}")
    return resolved.relative_to(root).as_posix()


def _map_path(artifact_root: Path) -> Path:
    return Path(artifact_root) / CHILD_RUN_MAP_FILENAME


def _read_document(artifact_root: Path) -> dict[str, Any] | None:
    path = _map_path(artifact_root)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChildRunMapError(f"invalid child-run map {path.name}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != CHILD_RUN_MAP_VERSION:
        raise ChildRunMapError(f"unsupported child-run map version in {path.name}")
    return data


def read_child_run_map(
    artifact_root: Path,
    *,
    destination: ChildRunDestination | None = None,
) -> dict[str, dict[str, str]]:
    """Read the persisted mapping, validating its destination scope.

    When ``destination`` is given, a map persisted for a different tracking
    server, experiment, or parent run raises :class:`ChildRunMapError`: a stale
    map must never attach children to the wrong parent.
    """
    data = _read_document(artifact_root)
    if data is None:
        return {}

    entries = data.get("children")
    if not isinstance(entries, dict):
        raise ChildRunMapError(
            f"child-run map {_map_path(artifact_root).name} is missing a 'children' mapping"
        )

    stored_scope = data.get("destination")
    if destination is not None and isinstance(stored_scope, dict):
        stored = ChildRunDestination.from_dict(stored_scope)
        if stored != destination:
            raise ChildRunMapError(
                "child-run map is scoped to a different export destination "
                f"(stored parent {stored.parent_run_id!r}, requested "
                f"{destination.parent_run_id!r}); refusing to resume children"
            )
    elif destination is not None and stored_scope is None:
        raise ChildRunMapError(
            "child-run map has no destination scope; refusing to resume children against "
            "an unscoped map"
        )

    result: dict[str, dict[str, str]] = {}
    for key, value in entries.items():
        if not isinstance(value, dict) or not value.get("run_id"):
            raise ChildRunMapError(f"child-run map entry {key!r} is missing run_id")
        result[str(key)] = {
            "run_id": str(value["run_id"]),
            "run_name": str(value.get("run_name", "")),
        }
    return result


def write_child_run_map(
    artifact_root: Path,
    mapping: dict[str, dict[str, str]],
    *,
    destination: ChildRunDestination,
) -> None:
    """Persist the mapping atomically at the artifact root, scoped to a destination."""
    path = _map_path(artifact_root)
    doc: dict[str, Any] = {
        "version": CHILD_RUN_MAP_VERSION,
        "destination": destination.to_dict(),
        "children": mapping,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "CHILD_RUN_MAP_FILENAME",
    "CHILD_RUN_MAP_VERSION",
    "ChildRunDestination",
    "ChildRunMapError",
    "child_run_key",
    "read_child_run_map",
    "write_child_run_map",
]
