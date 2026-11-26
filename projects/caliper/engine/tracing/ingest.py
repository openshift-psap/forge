"""Orchestrate native ingestion of every trace bundle under an artifact root.

Given an artifact root and a way to resolve each bundle's owning run, this
module discovers bundles, ingests and links each one, and aggregates outcomes
and failures. Ordinary artifact upload is unaffected: native ingestion is a
separate, independently-failing step.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from projects.caliper.engine.tracing import bundle as bundle_mod
from projects.caliper.engine.tracing.analysis import TraceValidationError
from projects.caliper.engine.tracing.exporter import (
    IngestionOutcome,
    TraceExportError,
    ingest_bundle,
    read_receipt,
)

logger = logging.getLogger(__name__)


@dataclass
class TraceExportContext:
    """Native trace ingestion is enabled for this export.

    The tracking URI, workspace, credentials and experiment ID are resolved by
    the export code that already knows them and are passed to ingestion
    explicitly, so this object carries no destination state that could be left
    stale or empty. ``receipt_dir`` is the export step's own directory for the
    per-bundle export receipts, so the exported test directories are never
    modified.
    """

    receipt_dir: Path


@dataclass
class IngestionReport:
    """Aggregate result of a native ingestion pass."""

    outcomes: list[IngestionOutcome] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    discovered: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "outcomes": [
                {
                    "test_id": o.test_id,
                    "status": o.status,
                    "ingested": o.ingested,
                    "linked": o.linked,
                    "run_id": o.run_id,
                }
                for o in self.outcomes
            ],
            "skipped": list(self.skipped),
            "failures": list(self.failures),
        }


def receipt_path_for(receipt_dir: Path, bundle_dir: Path, artifact_root: Path) -> Path:
    """Place a bundle's export receipt under ``receipt_dir``, mirroring its path."""
    relative = bundle_dir.resolve().relative_to(Path(artifact_root).resolve())
    return Path(receipt_dir) / relative / bundle_mod.EXPORT_RECEIPT_FILENAME


def _bundle_label(summary_path: Path, artifact_root: Path) -> str:
    """Name a bundle before its summary is known to be valid."""
    try:
        return summary_path.parent.relative_to(artifact_root).as_posix()
    except ValueError:
        return summary_path.parent.name


def ingest_trace_bundles(
    *,
    artifact_root: Path,
    tracking_uri: str,
    experiment_id: str,
    workspace: str,
    connection: dict[str, Any] | None,
    resolve_run_id: Callable[[Path], str | None],
    receipt_dir: Path,
    reporter: Callable[[str], None] | None = None,
) -> IngestionReport:
    """Discover and ingest every trace bundle under ``artifact_root``.

    ``resolve_run_id(test_dir)`` returns the MLflow run ID owning that bundle's
    test node, or ``None`` if no run exists (recorded as skipped, never a silent
    success). A bundle that fails ingestion raises no further work for that
    bundle; other bundles still attempt, and failures are aggregated.
    """
    report = IngestionReport()
    summary_paths = bundle_mod.find_bundle_summaries(artifact_root)
    report.discovered = len(summary_paths)

    if not summary_paths:
        # Enabling native export without a bundle is a clear skipped result,
        # not an empty request.
        report.skipped.append("no trace bundles found")
        if reporter:
            reporter("trace export: no bundles found; skipping native ingestion")
        return report

    seen_archives: set[Path] = set()
    for summary_path in summary_paths:
        # Each bundle is validated and read inside its own error boundary, so
        # one missing archive, malformed summary or corrupt archive fails only
        # that bundle; the other workloads still export.
        label = _bundle_label(summary_path, artifact_root)
        try:
            bundle = bundle_mod.validate_bundle(summary_path, artifact_root=Path(artifact_root))
            label = bundle.test_id
            resolved_archive = bundle.archive_path.resolve()
            if resolved_archive in seen_archives:
                continue
            seen_archives.add(resolved_archive)

            run_id = resolve_run_id(bundle.test_dir)
            if not run_id:
                reason = f"no MLflow run resolved for test '{bundle.test_id}'"
                report.skipped.append(reason)
                logger.warning("Trace export skipped: %s", reason)
                if reporter:
                    reporter(f"trace export: skipped {bundle.test_id} ({reason})")
                continue

            archive_sha = bundle_mod.sha256_of_file(bundle.archive_path)
            receipt_path = receipt_path_for(receipt_dir, bundle.bundle_dir, artifact_root)
            existing = read_receipt(receipt_path)
            outcome = ingest_bundle(
                bundle=bundle,
                run_id=run_id,
                tracking_uri=tracking_uri,
                experiment_id=experiment_id,
                workspace=workspace,
                connection=connection,
                archive_sha256=archive_sha,
                receipt_path=receipt_path,
                existing_receipt=existing,
            )
            report.outcomes.append(outcome)
            if reporter:
                reporter(
                    f"trace export: {outcome.test_id} ingested {outcome.ingested} "
                    f"linked {outcome.linked} (run {outcome.run_id})"
                )
        except TraceExportError as exc:
            # The message is a sanitized category; never the raw payload.
            report.failures.append(f"{label}: {exc}")
            logger.error("Trace export failed for %s", label)
        except bundle_mod.BundleValidationError as exc:
            report.failures.append(f"{label}: {exc}")
            logger.error("Trace bundle validation failed for %s", summary_path)
        except (TraceValidationError, OSError, EOFError, UnicodeDecodeError) as exc:
            # A corrupt or unreadable archive. Only the error type is reported:
            # decoder messages can quote archive content.
            report.failures.append(f"{label}: unreadable trace archive ({type(exc).__name__})")
            logger.error("Trace archive unreadable for %s (%s)", label, type(exc).__name__)

    return report
