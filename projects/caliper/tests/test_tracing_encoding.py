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
    iter_record_batches,
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


OTHER_TRACE_ID = "5bf92f3577b34da6a3ce929d0e0e4736"


def _record_with_trace(trace_hex: str, service: str) -> dict:
    record = _record()
    resource_spans = record["resourceSpans"][0]
    resource_spans["resource"]["attributes"] = [
        {"key": "service.name", "value": {"stringValue": service}}
    ]
    for span in resource_spans["scopeSpans"][0]["spans"]:
        span["traceId"] = trace_hex
    return record


def test_encode_merges_every_record_not_just_the_last():
    """Parse() clears its target, so encoding must merge per-record messages.

    A spool is one record per collector flush; a naive single-message parse
    keeps only the final record and silently drops the rest.
    """
    encoded = encode_records_to_request(
        [_record_with_trace(HEX_TRACE_ID, "svc-a"), _record_with_trace(OTHER_TRACE_ID, "svc-b")]
    )
    request = _decode(encoded.body)
    resource_spans = request.resource_spans
    assert len(resource_spans) == 2
    services = {
        a.value.string_value
        for rs in resource_spans
        for a in rs.resource.attributes
        if a.key == "service.name"
    }
    assert services == {"svc-a", "svc-b"}
    # Both traces are reported, not only the last one.
    assert set(encoded.trace_ids) == {HEX_TRACE_ID, OTHER_TRACE_ID}


def test_iter_record_batches_is_bounded():
    records = [_record() for _ in range(5)]
    batches = list(iter_record_batches(records, max_records=2))
    assert [len(b) for b in batches] == [2, 2, 1]
