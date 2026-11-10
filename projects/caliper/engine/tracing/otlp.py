"""Build OTLP/JSON export requests from sanitized OTLP JSON records.

The collector's file exporter emits newline-delimited OTLP JSON, which MLflow's
``/v1/traces`` endpoint accepts directly as ``application/json``: the server
converts the hex ``traceId``/``spanId``/``parentSpanId`` fields itself. See
https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding.

Span links are left out of the request. MLflow does not store them, and it does
not convert their hex IDs, so sending them would only risk a mis-decoded or
rejected request. They remain in the archive artifact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

# Trace ids are 16 bytes, span ids 8 bytes, as lowercase or uppercase hex.
_ID_HEX_LENGTHS = {"traceId": 32, "spanId": 16, "parentSpanId": 16}
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class OtlpEncodingError(ValueError):
    """Raised when an OTLP JSON record cannot be sent as an export request."""


@dataclass
class EncodedBatch:
    """Result of encoding one group of records."""

    body: bytes
    trace_ids: list[str] = field(default_factory=list)


# Records per POST. The collector spool holds one export request per flush, so
# a capture can be many records; batching bounds request size and keeps a single
# malformed record from discarding the whole archive.
MAX_RECORDS_PER_REQUEST = 500


def _validate_id(span: dict[str, Any], field_name: str) -> str | None:
    value = span.get(field_name)
    if not value:
        if field_name == "parentSpanId":
            return None
        raise OtlpEncodingError(f"span is missing {field_name!r}")
    if not isinstance(value, str) or not _HEX_RE.match(value):
        raise OtlpEncodingError(f"span {field_name!r} must be a hex string")
    if len(value) != _ID_HEX_LENGTHS[field_name]:
        raise OtlpEncodingError(
            f"span {field_name!r} must be {_ID_HEX_LENGTHS[field_name]} hex characters, "
            f"got {len(value)}"
        )
    return value.lower()


def encode_records_to_request(records: list[dict[str, Any]]) -> EncodedBatch:
    """Merge OTLP JSON records into one ``application/json`` export request.

    Returns the serialized request plus the distinct OTLP trace IDs (lowercase
    hex) it contains, so callers can derive MLflow trace IDs.
    """
    resource_spans: list[dict[str, Any]] = []
    trace_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise OtlpEncodingError("OTLP JSON record must be an object")
        for resource_span in record.get("resourceSpans") or []:
            scope_spans = []
            for scope_span in resource_span.get("scopeSpans") or []:
                spans = []
                for span in scope_span.get("spans") or []:
                    trace_ids.append(_validate_id(span, "traceId"))
                    _validate_id(span, "spanId")
                    _validate_id(span, "parentSpanId")
                    spans.append({key: value for key, value in span.items() if key != "links"})
                scope_spans.append({**scope_span, "spans": spans})
            resource_spans.append({**resource_span, "scopeSpans": scope_spans})

    if not trace_ids:
        raise OtlpEncodingError("OTLP JSON batch contains no spans")

    body = json.dumps({"resourceSpans": resource_spans}).encode("utf-8")
    return EncodedBatch(body=body, trace_ids=list(dict.fromkeys(trace_ids)))


def iter_record_batches(
    records: Iterable[dict[str, Any]], *, max_records: int | None = None
) -> Iterator[list[dict[str, Any]]]:
    """Split records into bounded batches for separate POSTs.

    Accepts any iterable so a caller can stream a large archive instead of
    materializing it: each batch is consumed and released in turn.
    """
    if max_records is None:
        max_records = MAX_RECORDS_PER_REQUEST
    batch: list[dict[str, Any]] = []
    for record in records:
        batch.append(record)
        if len(batch) >= max_records:
            yield batch
            batch = []
    if batch:
        yield batch
