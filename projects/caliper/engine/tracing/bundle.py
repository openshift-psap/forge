"""Discovery and validation of Caliper trace bundles.

A trace bundle is a directory containing ``trace-summary.json`` and the
sanitized OTLP archive it describes. Bundles are discovered by scanning for
versioned summaries under a Caliper artifact root and assigning each to its
nearest owning ``__caliper_test_metadata__.yaml`` test node.

Everything is validation-only here: no network, no configuration. This runs
inside the collection toolbox and again during export.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projects.caliper.engine.constants import LEGACY_METADATA_FILE, METADATA_FILE

# The bundle filename convention. Versioned so future schema changes are explicit.
SUMMARY_FILENAME = "trace-summary.json"
ARCHIVE_FILENAME = "otlp.jsonl.gz"
EXPORT_RECEIPT_FILENAME = "mlflow-export.json"

# Markers that identify a Caliper test node (parent of the numbered command dir).
_TEST_NODE_MARKERS = (METADATA_FILE, LEGACY_METADATA_FILE)


class BundleValidationError(ValueError):
    """Raised when a bundle summary or archive is missing or malformed."""


@dataclass(frozen=True)
class TraceBundle:
    """A validated trace bundle on disk."""

    summary_path: Path
    archive_path: Path
    test_dir: Path
    test_id: str
    summary: dict[str, Any]

    @property
    def bundle_dir(self) -> Path:
        return self.summary_path.parent


def _load_summary(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"cannot read trace summary {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise BundleValidationError(f"trace summary {path.name} must be a JSON object")
    return data


def _require_version(summary: dict[str, Any]) -> int:
    version = summary.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise BundleValidationError("trace summary is missing an integer schema_version")
    from projects.cluster.library.tracing.models import SUMMARY_SCHEMA_VERSION

    if version != SUMMARY_SCHEMA_VERSION:
        raise BundleValidationError(
            f"unsupported trace summary schema_version {version} "
            f"(expected {SUMMARY_SCHEMA_VERSION})"
        )
    return version


def _owning_test_dir(bundle_dir: Path, artifact_root: Path) -> Path:
    """Return the nearest ancestor holding a Caliper test-node marker.

    Guards against traversal: the resolved owner must stay under the resolved
    artifact root.
    """
    root = artifact_root.resolve()
    for candidate in [bundle_dir.resolve(), *bundle_dir.resolve().parents]:
        if not candidate.is_relative_to(root):
            break
        if any((candidate / marker).is_file() for marker in _TEST_NODE_MARKERS):
            return candidate
    raise BundleValidationError(f"no owning Caliper test node found above {bundle_dir}")


def validate_bundle(summary_path: Path, *, artifact_root: Path) -> TraceBundle:
    """Load and validate one bundle summary + archive pair."""
    summary = _load_summary(summary_path)
    _require_version(summary)

    identity = summary.get("identity")
    if not isinstance(identity, dict) or not identity.get("test_id"):
        raise BundleValidationError("trace summary identity.test_id is required")

    data = summary.get("data")
    if not isinstance(data, dict):
        raise BundleValidationError("trace summary data block is required")
    relative = data.get("archive_relative_path")
    if not isinstance(relative, str) or not relative:
        raise BundleValidationError("trace summary data.archive_relative_path is required")

    # Prevent parent traversal in the recorded relative path.
    rel_path = Path(relative)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise BundleValidationError("archive_relative_path must be a safe relative path")

    archive_path = (summary_path.parent / rel_path).resolve()
    if not archive_path.is_relative_to(summary_path.parent.resolve()):
        raise BundleValidationError("archive_relative_path escapes the bundle directory")
    if not archive_path.is_file():
        raise BundleValidationError(f"trace archive not found: {rel_path}")

    # Symlink escape check: the archive must not resolve outside the bundle dir.
    test_dir = _owning_test_dir(summary_path.parent, artifact_root)

    return TraceBundle(
        summary_path=summary_path,
        archive_path=archive_path,
        test_dir=test_dir,
        test_id=str(identity["test_id"]),
        summary=summary,
    )


def discover_bundles(artifact_root: Path) -> list[TraceBundle]:
    """Discover all validated bundles beneath ``artifact_root``.

    Duplicate discovery (the same archive reached twice) is impossible here as
    summaries are unique files; a bundle whose summary is invalid raises rather
    than being skipped silently.
    """
    root = Path(artifact_root)
    bundles: list[TraceBundle] = []
    seen_archives: set[Path] = set()
    for summary_path in sorted(root.rglob(SUMMARY_FILENAME)):
        bundle = validate_bundle(summary_path, artifact_root=root)
        resolved_archive = bundle.archive_path.resolve()
        if resolved_archive in seen_archives:
            continue
        seen_archives.add(resolved_archive)
        bundles.append(bundle)
    return bundles


def sha256_of_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file's SHA-256 (raw stored bytes) without loading it into memory.

    This hashes the file as written/copied, which is what a transfer verifies.
    Decompression happens separately where archive content is read.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ARCHIVE_FILENAME",
    "EXPORT_RECEIPT_FILENAME",
    "SUMMARY_FILENAME",
    "BundleValidationError",
    "TraceBundle",
    "discover_bundles",
    "sha256_of_file",
    "validate_bundle",
]
