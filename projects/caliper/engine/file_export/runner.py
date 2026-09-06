"""Coordinate multi-backend file export (FR-017)."""

from __future__ import annotations

import fnmatch
import sys
import traceback
from pathlib import Path
from typing import Any

from projects.caliper.engine.file_export import mlflow_backend
from projects.caliper.engine.model import FileExportBackendResult


def apply_redaction_policy(paths: list[Path], policy: dict[str, Any] | None) -> list[Path]:
    """FR-013 hook: placeholder returns paths unchanged unless policy excludes globs."""
    if not policy:
        return paths
    exclude = policy.get("exclude_globs", [])
    if not exclude:
        return paths
    out: list[Path] = []
    for p in paths:
        if p.is_file() and not any(
            fnmatch.fnmatch(p.name, g) or fnmatch.fnmatch(str(p), g) for g in exclude
        ):
            out.append(p)
    return out


def run_file_export(
    *,
    source: Path,
    backends: list[str],
    dry_run: bool,
    mlflow_tracking_uri: str | None,
    mlflow_experiment: str | None,
    mlflow_run_id: str | None,
    mlflow_run_name: str | None = None,
    mlflow_insecure_tls: bool = False,
    mlflow_connection: dict[str, Any] | None = None,
    redaction_policy: dict[str, Any] | None = None,
    verbose: bool = False,
    upload_workers: int = 10,
    mlflow_run_metadata: dict[str, Any] | None = None,
    mlflow_workspace: str | None = None,
) -> list[FileExportBackendResult]:
    if source.is_file():
        paths = [source]
    else:
        paths = [p for p in source.rglob("*") if p.is_file()]
    paths = apply_redaction_policy(paths, redaction_policy)
    if verbose:
        print(
            f"caliper: collected {len(paths)} file(s) under {source}",
            file=sys.stderr,
        )
        if len(paths) > 12:
            for p in paths[:5]:
                print(f"caliper:   … {p}", file=sys.stderr)
            print(f"caliper:   … ({len(paths) - 10} more paths omitted)", file=sys.stderr)
            for p in paths[-5:]:
                print(f"caliper:   … {p}", file=sys.stderr)
        else:
            for p in paths:
                print(f"caliper:   … {p}", file=sys.stderr)
    results: list[FileExportBackendResult] = []
    for b in backends:
        if b not in ("mlflow",):
            results.append(
                FileExportBackendResult(backend=b, status="skipped", detail="unknown backend")
            )
            continue
        if dry_run:
            if verbose:
                print(
                    f"caliper: dry-run — would run backend {b!r} on {len(paths)} file(s)",
                    file=sys.stderr,
                )
            results.append(
                FileExportBackendResult(
                    backend=b, status="skipped", detail=f"dry-run would upload {len(paths)} files"
                )
            )
            continue
        try:
            if verbose:
                print(f"caliper: starting backend {b!r} …", file=sys.stderr)
            detail, ml_meta, failures = mlflow_backend.log_artifacts(
                artifact_root=source,
                paths=paths,
                tracking_uri=mlflow_tracking_uri,
                experiment=mlflow_experiment,
                run_id=mlflow_run_id,
                run_name=mlflow_run_name,
                insecure_tls=mlflow_insecure_tls,
                connection=mlflow_connection,
                verbose=verbose,
                upload_workers=upload_workers,
                run_metadata=mlflow_run_metadata,
                workspace=mlflow_workspace,
            )
            results.append(
                FileExportBackendResult(
                    backend="mlflow",
                    status="success" if not failures else "failure",
                    detail=detail,
                    metadata=ml_meta,
                )
            )
            if verbose:
                print(f"caliper: backend {b!r} finished ({detail})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            # Full chain on stderr; stdout line stays a short message for automation.
            traceback.print_exception(e, file=sys.stderr)
            print(
                f"caliper: backend {b!r} failed: {e}",
                file=sys.stderr,
            )
            results.append(FileExportBackendResult(backend=b, status="failure", detail=str(e)))
    return results
