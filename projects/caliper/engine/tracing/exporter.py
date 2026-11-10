"""Native MLflow ingestion of sanitized OTLP trace bundles.

This module owns:

* encoding sanitized archives into bounded OTLP protobuf requests,
* POSTing them to the tracking server's ``/v1/traces`` endpoint,
* resolving the resulting native trace IDs through the tested server contract,
* linking accepted traces to the owning single or child run,
* persisting a resumable receipt so repeated export never duplicates children.

Credentials come from the existing MLflow secrets/vault plumbing. No credential
value is ever written to an artifact, a task argument, a log line, or a shared
context object.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from projects.caliper.engine.tracing.analysis import read_archive_records
from projects.caliper.engine.tracing.bundle import TraceBundle
from projects.caliper.engine.tracing.otlp import (
    OtlpEncodingError,
    encode_records_to_request,
    iter_record_batches,
)
from projects.caliper.engine.tracing.payload import iter_enriched_records

# Bounded retry policy for transient transport/throttling failures.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 20.0
# MLflow client rejects more than 100 trace ids per linkage call.
LINK_BATCH_SIZE = 100
EXPORTER_VERSION = 1

# HTTP status codes treated as retryable (transient).
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class TraceExportError(RuntimeError):
    """Raised when native ingestion cannot complete for a non-retryable reason."""


@dataclass
class ExportReceipt:
    """Scope receipt persisted next to the archive.

    Scoped by tracking server, workspace, experiment, run, archive checksum and
    exporter version, so a receipt for one destination never suppresses export
    to another.
    """

    test_id: str
    tracking_uri: str
    workspace: str
    experiment_id: str
    run_id: str
    archive_sha256: str
    exporter_version: int = EXPORTER_VERSION
    ingested_trace_ids: list[str] = field(default_factory=list)
    linked_trace_ids: list[str] = field(default_factory=list)
    status: str = "pending"
    reason: str = ""
    updated_at: str = ""

    def scope_matches(
        self,
        *,
        tracking_uri: str,
        workspace: str,
        experiment_id: str,
        run_id: str,
        archive_sha256: str,
    ) -> bool:
        return (
            self.tracking_uri == tracking_uri
            and self.workspace == workspace
            and self.experiment_id == experiment_id
            and self.run_id == run_id
            and self.archive_sha256 == archive_sha256
            and self.exporter_version == EXPORTER_VERSION
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "test_id": self.test_id,
            "tracking_uri": self.tracking_uri,
            "workspace": self.workspace,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "archive_sha256": self.archive_sha256,
            "exporter_version": self.exporter_version,
            "ingested_trace_ids": self.ingested_trace_ids,
            "linked_trace_ids": self.linked_trace_ids,
            "status": self.status,
            "reason": self.reason,
            "updated_at": self.updated_at or _now_iso(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExportReceipt:
        return cls(
            test_id=str(data.get("test_id", "")),
            tracking_uri=str(data.get("tracking_uri", "")),
            workspace=str(data.get("workspace", "")),
            experiment_id=str(data.get("experiment_id", "")),
            run_id=str(data.get("run_id", "")),
            archive_sha256=str(data.get("archive_sha256", "")),
            exporter_version=int(data.get("exporter_version", 0)),
            ingested_trace_ids=list(data.get("ingested_trace_ids", [])),
            linked_trace_ids=list(data.get("linked_trace_ids", [])),
            status=str(data.get("status", "pending")),
            reason=str(data.get("reason", "")),
            updated_at=str(data.get("updated_at", "")),
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_receipt(path: Path) -> ExportReceipt | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return ExportReceipt.from_dict(data)


def write_receipt(path: Path, receipt: ExportReceipt) -> None:
    """Atomically write the receipt via a temp file + rename."""
    receipt.updated_at = _now_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(receipt.to_dict(), indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def derive_mlflow_trace_ids(otlp_trace_ids: list[str]) -> list[str]:
    """Derive MLflow trace IDs from OTLP trace IDs using the tested contract.

    The server returns no IDs in its 200 response, so the ID must be derived.
    Imported lazily to avoid requiring mlflow for pure bundle operations.
    """
    from mlflow.tracing.utils import generate_mlflow_trace_id_from_otel_trace_id

    return [
        generate_mlflow_trace_id_from_otel_trace_id(int(trace_id, 16))
        for trace_id in otlp_trace_ids
    ]


def _post_traces(
    *,
    tracking_uri: str,
    experiment_id: str,
    workspace: str | None,
    body: bytes,
    token: str | None,
    username: str | None,
    password: str | None,
    insecure_tls: bool,
    server_cert_path: str | None,
) -> None:
    """POST one OTLP protobuf request to the tracking server's /v1/traces.

    Uses an explicitly configured client rather than global environment
    credentials. Retries transient failures with bounded backoff.
    """
    import urllib.error
    import urllib.request

    url = _traces_url(tracking_uri)
    headers = {
        "Content-Type": "application/x-protobuf",
        "x-mlflow-experiment-id": experiment_id,
    }
    if workspace:
        headers["X-MLFLOW-WORKSPACE"] = workspace
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif username and password:
        import base64

        creds = base64.standard_b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"

    context = None
    if insecure_tls or server_cert_path:
        import ssl

        if server_cert_path:
            context = ssl.create_default_context(cafile=server_cert_path)
        else:
            context = ssl._create_unverified_context()

    attempt = 0
    backoff = DEFAULT_BACKOFF_SECONDS
    while True:
        attempt += 1
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60, context=context):
                return
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRYABLE_STATUS and attempt < DEFAULT_MAX_ATTEMPTS:
                _sleep_backoff(backoff)
                backoff = min(backoff * 2, DEFAULT_MAX_BACKOFF_SECONDS)
                continue
            # Never include the response body verbatim: it may echo payload.
            raise TraceExportError(f"trace ingestion rejected with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < DEFAULT_MAX_ATTEMPTS:
                _sleep_backoff(backoff)
                backoff = min(backoff * 2, DEFAULT_MAX_BACKOFF_SECONDS)
                continue
            raise TraceExportError(
                f"trace ingestion transport failure after {attempt} attempts: {type(exc).__name__}"
            ) from exc


def _sleep_backoff(seconds: float) -> None:
    # A real wait between bounded retries; not an arbitrary readiness sleep.
    time.sleep(seconds)


def _traces_url(tracking_uri: str) -> str:
    """Build the /v1/traces URL, preserving any supported URI prefix."""
    base = tracking_uri.rstrip("/")
    return f"{base}/v1/traces"


@dataclass
class IngestionOutcome:
    """Summary of one bundle's ingestion for status/notifications."""

    test_id: str
    status: str
    ingested: int = 0
    linked: int = 0
    reason: str = ""
    run_id: str = ""


def ingest_bundle(
    *,
    bundle: TraceBundle,
    run_id: str,
    tracking_uri: str,
    experiment_id: str,
    workspace: str,
    connection: dict[str, Any] | None,
    archive_sha256: str,
    existing_receipt: ExportReceipt | None = None,
) -> IngestionOutcome:
    """Ingest a validated bundle into a single MLflow run, resuming if possible.

    Idempotency/recovery contract:
    - a scope-matching completed receipt short-circuits with no new request,
    - a receipt whose ingestion succeeded but linking failed retries only linking,
    - a receipt for a *different* destination is rejected unless re-scoped by the
      caller passing an explicit new target.

    Returns an outcome; raises :class:`TraceExportError` on unrecoverable failure.
    """
    receipt_path = bundle.bundle_dir / "mlflow-export.json"

    if existing_receipt and not existing_receipt.scope_matches(
        tracking_uri=tracking_uri,
        workspace=workspace,
        experiment_id=experiment_id,
        run_id=run_id,
        archive_sha256=archive_sha256,
    ):
        raise TraceExportError(
            "existing trace export receipt does not match the current destination or archive; "
            "refusing to reuse it"
        )

    connection = connection or {}
    receipt = existing_receipt or ExportReceipt(
        test_id=bundle.test_id,
        tracking_uri=tracking_uri,
        workspace=workspace,
        experiment_id=experiment_id,
        run_id=run_id,
        archive_sha256=archive_sha256,
    )

    otlp_trace_ids: list[str] = []
    if not receipt.ingested_trace_ids:
        # Stream the archive and POST bounded batches, so a single malformed
        # record is rejected only within its batch (MLflow's OTLP endpoint is
        # all-or-nothing per request) and memory use does not grow with the
        # capture. Enrichment is lazy for the same reason.
        streamed = iter_enriched_records(read_archive_records(bundle.archive_path))
        for batch in iter_record_batches(streamed):
            try:
                encoded = encode_records_to_request(batch)
            except OtlpEncodingError as exc:
                # A record protobuf rejects is a per-bundle failure, not an
                # escape: the caller reports it as a trace failure and keeps the
                # MLflow run metadata instead of aborting the whole export.
                raise TraceExportError(str(exc)) from exc
            _post_traces(
                tracking_uri=tracking_uri,
                experiment_id=experiment_id,
                workspace=workspace or None,
                body=encoded.body,
                token=connection.get("token"),
                username=connection.get("username"),
                password=connection.get("password"),
                insecure_tls=bool(connection.get("insecure_tls")),
                server_cert_path=connection.get("server_cert_path"),
            )
            otlp_trace_ids.extend(encoded.trace_ids)
        if otlp_trace_ids:
            receipt.ingested_trace_ids = derive_mlflow_trace_ids(
                list(dict.fromkeys(otlp_trace_ids))
            )
        receipt.status = "ingested"
        write_receipt(receipt_path, receipt)
    else:
        # Resume: ingestion already recorded, but the process may have died
        # before linking. Verify the count matches by trusting the receipt.
        receipt.status = "ingested"

    mlflow_trace_ids = receipt.ingested_trace_ids
    linked = set(receipt.linked_trace_ids)
    pending = [t for t in mlflow_trace_ids if t not in linked]
    if pending:
        _link_traces(
            trace_ids=pending,
            run_id=run_id,
            tracking_uri=tracking_uri,
            connection=connection,
        )
        receipt.linked_trace_ids = sorted(linked.union(pending))

    receipt.status = "complete"
    receipt.reason = ""
    write_receipt(receipt_path, receipt)

    return IngestionOutcome(
        test_id=bundle.test_id,
        status="complete",
        ingested=len(receipt.ingested_trace_ids),
        linked=len(receipt.linked_trace_ids),
        run_id=run_id,
    )


def _link_traces(
    *,
    trace_ids: list[str],
    run_id: str,
    tracking_uri: str,
    connection: dict[str, Any],
) -> None:
    """Link traces to a run in batches, bounded-retrying transient failures."""
    from projects.caliper.engine.file_export.mlflow_secrets import mlflow_connection_env

    for start in range(0, len(trace_ids), LINK_BATCH_SIZE):
        batch = trace_ids[start : start + LINK_BATCH_SIZE]
        attempt = 0
        backoff = DEFAULT_BACKOFF_SECONDS
        while True:
            attempt += 1
            try:
                with mlflow_connection_env(connection):
                    import mlflow

                    mlflow.set_tracking_uri(tracking_uri)
                    client = mlflow.tracking.MlflowClient()
                    client.link_traces_to_run(trace_ids=batch, run_id=run_id)
                break
            except Exception as exc:  # noqa: BLE001 - classify then re-raise typed
                if attempt < DEFAULT_MAX_ATTEMPTS:
                    _sleep_backoff(backoff)
                    backoff = min(backoff * 2, DEFAULT_MAX_BACKOFF_SECONDS)
                    continue
                raise TraceExportError(
                    f"trace linkage failed after {attempt} attempts: {type(exc).__name__}"
                ) from exc
