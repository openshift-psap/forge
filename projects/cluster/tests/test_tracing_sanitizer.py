"""Sanitization tests: sentinels never survive into any persisted surface.

The plan requires sanitization *before* the spool write, and verification by
decoding compressed artifacts. These tests run sentinel values through the
sanitizer and assert they are absent from the resulting record — the same
function the capture toolbox runs before persisting.
"""

from __future__ import annotations

import json

from projects.cluster.library.tracing.sanitizer import (
    REDACTED_EVENT,
    REDACTED_NAME,
    RESOURCE_ATTRIBUTE_ALLOWLIST,
    SANITIZATION_POLICY_VERSION,
    SPAN_ATTRIBUTE_ALLOWLIST,
    is_allowed_attribute_key,
    redact_anyvalue,
    redact_value,
    sanitize_export_request,
    sanitize_span,
)

SENTINELS = {
    "prompt": "SENTINEL_PROMPT_do_not_persist",
    "completion": "SENTINEL_COMPLETION_do_not_persist",
    "authorization": "Bearer SENTINEL_TOKEN_abcdef123456",
    "cookie": "session=SENTINEL_COOKIE",
    "url": "https://user:SENTINEL_PASSWORD@host.example.com/v1?token=SENTINEL_QUERY",
    "exception": "SENTINEL_EXCEPTION_traceback_text",
}


def _record_with_sentinels() -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "forge-llm"}},
                        {"key": "gen_ai.prompt", "value": {"stringValue": SENTINELS["prompt"]}},
                        {
                            "key": "http.request.header.authorization",
                            "value": {"stringValue": SENTINELS["authorization"]},
                        },
                        {"key": "url.full", "value": {"stringValue": SENTINELS["url"]}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "vllm"},
                        "spans": [
                            {
                                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                                "spanId": "00f067aa0ba902b7",
                                "name": f"prompt {SENTINELS['prompt']}",
                                "attributes": [
                                    {
                                        "key": "gen_ai.completion",
                                        "value": {"stringValue": SENTINELS["completion"]},
                                    },
                                    {
                                        "key": "http.request.method",
                                        "value": {"stringValue": "POST"},
                                    },
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": "42"},
                                    },
                                    {
                                        "key": "exception.message",
                                        "value": {"stringValue": SENTINELS["exception"]},
                                    },
                                ],
                                "status": {
                                    "code": 2,
                                    "message": f"error: {SENTINELS['exception']}",
                                },
                                "events": [
                                    {
                                        "name": f"completion {SENTINELS['completion']}",
                                        "attributes": [
                                            {
                                                "key": "cookie",
                                                "value": {"stringValue": SENTINELS["cookie"]},
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_sentinels_absent_from_sanitized_record():
    sanitized = sanitize_export_request(_record_with_sentinels())
    serialized = json.dumps(sanitized)
    for name, value in SENTINELS.items():
        assert value not in serialized, f"sentinel {name} leaked"


def test_allowed_keys_survive():
    sanitized = sanitize_export_request(_record_with_sentinels())
    span = sanitized["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    keys = {attr["key"] for attr in span["attributes"]}
    assert "http.request.method" in keys
    assert "gen_ai.usage.output_tokens" in keys
    values = {attr["key"]: attr["value"] for attr in span["attributes"]}
    assert values["gen_ai.usage.output_tokens"] == {"intValue": "42"}


def test_span_name_with_content_is_redacted():
    sanitized = sanitize_export_request(_record_with_sentinels())
    span = sanitized["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["name"] == REDACTED_NAME


def test_status_message_is_dropped():
    sanitized = sanitize_export_request(_record_with_sentinels())
    span = sanitized["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["status"] == {"code": 2}


def test_event_name_and_attributes_are_sanitized():
    sanitized = sanitize_export_request(_record_with_sentinels())
    event = sanitized["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"][0]
    assert event["name"] == REDACTED_EVENT
    assert event["attributes"] == []


def test_resource_allowlist_keeps_service_name_only():
    sanitized = sanitize_export_request(_record_with_sentinels())
    resource_keys = {
        attr["key"] for attr in sanitized["resourceSpans"][0]["resource"]["attributes"]
    }
    assert resource_keys == {"service.name"}
    assert resource_keys <= RESOURCE_ATTRIBUTE_ALLOWLIST


def test_sensitive_key_names_are_never_allowed():
    for key in ("authorization", "api_key", "password", "secret", "token", "prompt", "cookie"):
        assert not is_allowed_attribute_key(key, SPAN_ATTRIBUTE_ALLOWLIST)
        assert not is_allowed_attribute_key(key, RESOURCE_ATTRIBUTE_ALLOWLIST)


def test_redact_value_drops_credentials_in_urls_and_bearers():
    assert "SENTINEL_PASSWORD" not in redact_value(SENTINELS["url"])
    assert redact_value(SENTINELS["authorization"]) != SENTINELS["authorization"]


def test_sanitize_does_not_mutate_input():
    original = _record_with_sentinels()
    snapshot = json.dumps(original)
    sanitize_export_request(original)
    assert json.dumps(original) == snapshot


def test_policy_version_is_recorded():
    assert isinstance(SANITIZATION_POLICY_VERSION, int)
    assert SANITIZATION_POLICY_VERSION >= 1


def test_redact_anyvalue_descends_into_wrapped_scalar():
    """Attribute values arrive wrapped ({"stringValue": ...}); redaction must
    descend into the wrapper, not silently return it unchanged."""
    wrapped = {"stringValue": SENTINELS["authorization"]}
    assert redact_anyvalue(wrapped)["stringValue"] == "<redacted>"

    url = {"stringValue": SENTINELS["url"]}
    redacted = redact_anyvalue(url)["stringValue"]
    assert "SENTINEL_PASSWORD" not in redacted
    assert "SENTINEL_QUERY" not in redacted

    # Non-string wrappers are preserved as-is.
    assert redact_anyvalue({"intValue": "42"}) == {"intValue": "42"}


def test_wrapped_attribute_values_are_redacted_in_sanitized_record():
    record = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "forge-llm"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "vllm"},
                        "spans": [
                            {
                                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                                "spanId": "00f067aa0ba902b7",
                                "name": "ok",
                                "attributes": [
                                    {"key": "url.full", "value": {"stringValue": SENTINELS["url"]}},
                                    {
                                        "key": "server.address",
                                        "value": {"stringValue": SENTINELS["authorization"]},
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    text = json.dumps(sanitize_export_request(record))
    assert "SENTINEL_PASSWORD" not in text
    assert "SENTINEL_QUERY" not in text
    assert "SENTINEL_TOKEN" not in text


def test_span_link_attributes_are_sanitized():
    """Links carry their own attributes; they must not be copied verbatim."""
    span = {
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
        "spanId": "00f067aa0ba902b7",
        "name": "op",
        "links": [
            {
                "traceId": "5bf92f3577b34da6a3ce929d0e0e4736",
                "spanId": "11f067aa0ba902b7",
                "attributes": [
                    {"key": "gen_ai.prompt", "value": {"stringValue": SENTINELS["prompt"]}},
                    {"key": "http.request.method", "value": {"stringValue": "POST"}},
                ],
            }
        ],
    }
    sanitized = sanitize_span(span)
    link_attrs = sanitized["links"][0]["attributes"]
    keys = {a["key"] for a in link_attrs}
    assert "gen_ai.prompt" not in keys
    assert "http.request.method" in keys
    assert SENTINELS["prompt"] not in json.dumps(sanitized)
