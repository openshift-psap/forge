"""Convert sanitized OTLP JSON records into OTLP protobuf requests.

The collector's file exporter emits newline-delimited OTLP JSON. OTLP JSON
encodes trace/span IDs as lowercase hex and 64-bit integers as strings, but
protobuf's canonical JSON mapping expects base64 for ``bytes`` fields. A generic
protobuf JSON parser therefore cannot consume the archive directly; IDs must be
converted first. See https://opentelemetry.io/docs/specs/otlp/.

Imports of ``opentelemetry``/``protobuf`` are lazy so that tracing-disabled
commands never require the optional tracing packages.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# OTLP JSON fields that are hex in JSON but base64 (bytes) in protobuf JSON.
_HEX_ID_FIELDS = ("traceId", "spanId", "parentSpanId")


class OtlpEncodingError(ValueError):
    """Raised when an OTLP JSON record cannot be encoded to protobuf."""


def _require_proto() -> Any:
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise OtlpEncodingError(
            "OpenTelemetry protobuf definitions are required for native trace export. "
            "Install with: pip install -e '.[caliper]'"
        ) from exc
    return ExportTraceServiceRequest


_HEX_RE = __import__("re").compile(r"^[0-9a-fA-F]+$")


# Trace ids are 16 bytes, span ids 8 bytes.
_TRACE_ID_BYTES = 16
_SPAN_ID_BYTES = 8


def _id_to_base64(value: str, field_name: str) -> str:
    """Convert an OTLP JSON id to the base64 form protobuf JSON expects.

    OTLP JSON (spec) uses lowercase hex; protobuf's canonical JSON uses base64.
    Files produced by tools that already emit canonical JSON are accepted as-is.
    The decoded length is validated so a malformed id is rejected rather than
    silently accepted.
    """
    if not isinstance(value, str):
        raise OtlpEncodingError(f"span {field_name!r} must be a string")
    if _HEX_RE.match(value):
        raw = bytes.fromhex(value)
    else:
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise OtlpEncodingError(f"span {field_name!r} is neither hex nor base64") from exc

    expected = _TRACE_ID_BYTES if field_name == "traceId" else _SPAN_ID_BYTES
    if len(raw) != expected:
        raise OtlpEncodingError(
            f"span {field_name!r} must decode to {expected} bytes, got {len(raw)}"
        )
    return base64.b64encode(raw).decode("ascii")


def _convert_hex_ids_to_base64(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``record`` with hex trace/span IDs converted to base64."""
    data = json.loads(json.dumps(record))  # deep copy without sharing nested state
    for resource_span in data.get("resourceSpans", []) or []:
        for scope_span in resource_span.get("scopeSpans", []) or []:
            for span in scope_span.get("spans", []) or []:
                for field_name in _HEX_ID_FIELDS:
                    raw_value = span.get(field_name)
                    if not raw_value:
                        continue
                    span[field_name] = _id_to_base64(raw_value, field_name)
    return data


@dataclass
class EncodedBatch:
    """Result of encoding one group of records."""

    body: bytes
    trace_ids: list[str] = field(default_factory=list)


def encode_records_to_request(records: list[dict[str, Any]]) -> EncodedBatch:
    """Encode one or more OTLP JSON records into a single protobuf request.

    Returns the serialized ``ExportTraceServiceRequest`` plus the distinct
    OTLP trace IDs (lowercase hex) contained in the records so callers can
    derive MLflow trace IDs without an untested internal mapping.
    """
    ExportTraceServiceRequest = _require_proto()
    from google.protobuf.json_format import Parse as ParseJsonProto

    request = ExportTraceServiceRequest()
    trace_ids: list[str] = []
    for record in records:
        converted = _convert_hex_ids_to_base64(record)
        try:
            ParseJsonProto(json.dumps(converted), request, ignore_unknown_fields=True)
        except Exception as exc:  # protobuf json errors are not one type
            raise OtlpEncodingError(f"invalid OTLP JSON record: {type(exc).__name__}") from exc

    for resource_span in request.resource_spans:
        for scope_span in resource_span.scope_spans:
            for span in scope_span.spans:
                trace_ids.append(span.trace_id.hex())
    # Preserve order but drop duplicates.
    return EncodedBatch(body=request.SerializeToString(), trace_ids=list(dict.fromkeys(trace_ids)))


def iter_jsonl_records(text: str) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects from newline-delimited OTLP JSON text."""
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise OtlpEncodingError(f"invalid JSON on OTLP spool line {line_number}") from exc
