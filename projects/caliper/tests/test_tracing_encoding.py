"""Actual OTLP encoding tests.

The plan's Encoding row requires proving hex IDs, nanosecond timestamps, typed
attributes, events, links, and resource/scope metadata survive encoding into a
real ``ExportTraceServiceRequest``. These tests decode the produced protobuf
back and inspect the fields.
"""

from __future__ import annotations

import pytest

from projects.caliper.engine.tracing.otlp import (
    OtlpEncodingError,
    encode_records_to_request,
    iter_jsonl_records,
)

HEX_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
HEX_SPAN_ID = "00f067aa0ba902b7"
HEX_CHILD_ID = "11f067aa0ba902b7"


def _record() -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "forge-llm"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "vllm", "version": "0.6.0"},
                        "spans": [
                            {
                                "traceId": HEX_TRACE_ID,
                                "spanId": HEX_SPAN_ID,
                                "name": "root",
                                "kind": 2,
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": "42"},
                                    },
                                    {"key": "flag", "value": {"boolValue": True}},
                                    {"key": "score", "value": {"doubleValue": 1.5}},
                                ],
                                "events": [
                                    {"name": "queued", "timeUnixNano": "1700000000000000001"}
                                ],
                            },
                            {
                                "traceId": HEX_TRACE_ID,
                                "spanId": HEX_CHILD_ID,
                                "parentSpanId": HEX_SPAN_ID,
                                "name": "child",
                                "startTimeUnixNano": "1700000000100000000",
                                "endTimeUnixNano": "1700000000900000000",
                            },
                        ],
                    }
                ],
            }
        ]
    }


def _decode(body: bytes):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    return request


def test_encode_preserves_ids_and_relationships():
    encoded = encode_records_to_request([_record()])
    request = _decode(encoded.body)
    spans = request.resource_spans[0].scope_spans[0].spans
    root = spans[0]
    child = spans[1]
    assert root.trace_id.hex() == HEX_TRACE_ID
    assert root.span_id.hex() == HEX_SPAN_ID
    assert child.parent_span_id.hex() == HEX_SPAN_ID
    assert root.parent_span_id == b""
    assert encoded.trace_ids == [HEX_TRACE_ID]


def test_encode_preserves_nanosecond_timestamps():
    request = _decode(encode_records_to_request([_record()]).body)
    root = request.resource_spans[0].scope_spans[0].spans[0]
    assert root.start_time_unix_nano == 1700000000000000000
    assert root.end_time_unix_nano == 1700000001000000000


def test_encode_preserves_typed_attributes():
    request = _decode(encode_records_to_request([_record()]).body)
    root = request.resource_spans[0].scope_spans[0].spans[0]
    attrs = {a.key: a.value for a in root.attributes}
    assert attrs["gen_ai.usage.output_tokens"].int_value == 42
    assert attrs["flag"].bool_value is True
    assert attrs["score"].double_value == 1.5


def test_encode_preserves_events_and_scope():
    request = _decode(encode_records_to_request([_record()]).body)
    scope_span = request.resource_spans[0].scope_spans[0]
    assert scope_span.scope.name == "vllm"
    assert scope_span.scope.version == "0.6.0"
    root = scope_span.spans[0]
    assert root.events[0].name == "queued"
    assert root.events[0].time_unix_nano == 1700000000000000001


def test_encode_accepts_canonical_base64_ids():
    """A record already in canonical protobuf JSON (base64 ids) still encodes."""
    import base64

    record = _record()
    span = record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["traceId"] = base64.b64encode(bytes.fromhex(HEX_TRACE_ID)).decode()
    span["spanId"] = base64.b64encode(bytes.fromhex(HEX_SPAN_ID)).decode()
    encoded = encode_records_to_request([record])
    request = _decode(encoded.body)
    assert request.resource_spans[0].scope_spans[0].spans[0].trace_id.hex() == HEX_TRACE_ID


def test_encode_rejects_bad_hex_id():
    record = _record()
    record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "zzzz"
    with pytest.raises(OtlpEncodingError):
        encode_records_to_request([record])


def test_iter_jsonl_records_parses_lines():
    text = '{"a": 1}\n\n{"b": 2}\n'
    assert list(iter_jsonl_records(text)) == [{"a": 1}, {"b": 2}]


def test_iter_jsonl_records_rejects_bad_line():
    with pytest.raises(OtlpEncodingError, match="invalid JSON"):
        list(iter_jsonl_records('{"a": 1}\nnot json\n'))
