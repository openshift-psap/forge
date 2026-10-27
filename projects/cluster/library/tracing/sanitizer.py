"""Sanitization of OTLP records before they are persisted anywhere.

The sanitizer operates on decoded OTLP JSON dictionaries (the format the
collector's file exporter emits). It is applied *before* the spool archive is
written, so prompts, completions, headers, cookies, URL credentials and
arbitrary exception text never reach disk, reports, or the native endpoint.

Policy: a reviewed allowlist. Only known-safe attribute keys survive;
everything else is dropped. Span names, status descriptions, and event names
are reviewed by prefix rules as well — an attribute-only filter is not enough.

This module is pure: it never reads configuration, vaults, or the network.
"""

from __future__ import annotations

import re
from typing import Any

from projects.cluster.library.tracing.models import SANITIZATION_POLICY_VERSION

SANITIZATION_POLICY = "forge-trace-allowlist"

# Resource attributes that may be retained verbatim.
RESOURCE_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "service.name",
        "service.namespace",
        "service.version",
        "telemetry.sdk.name",
        "telemetry.sdk.language",
        "telemetry.sdk.version",
        "forge.job_id",
        "forge.capture_id",
        "forge.project",
        "forge.namespace",
        "forge.deployment_id",
        "forge.component.role",
        "k8s.namespace.name",
        "k8s.pod.name",
        "k8s.container.name",
        "k8s.deployment.name",
    }
)

# Span attribute keys retained verbatim (exact match).
SPAN_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "url.scheme",
        "server.address",
        "server.port",
        "network.protocol.version",
        "rpc.system",
        "rpc.service",
        "rpc.method",
        "gen_ai.system",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.request.max_tokens",
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.total_tokens",
        "llm.request.type",
        "error.type",
        "forge.job_id",
        "forge.capture_id",
        "forge.project",
        "forge.deployment_id",
        "forge.component.role",
        "forge.sanitization_policy_version",
    }
)

# Reviewed regular-expression allowlist for families of numeric/diagnostic keys.
SPAN_ATTRIBUTE_PATTERNS = (
    re.compile(r"^vllm\.[a-z0-9_.]*?(num_|count|tokens|duration|latency|size|queue|batch)"),
    re.compile(r"^llm_d\.[a-z0-9_.]*?(score|weight|queue|latency|duration|count)"),
    re.compile(r"^rhaiis\.[a-z0-9_.]*?(score|weight|queue|latency|duration|count)"),
    re.compile(r"^forge\.quality\.[a-z0-9_.]+$"),
)

# Keys that must never be retained even if a pattern would match them.
SENSITIVE_ATTRIBUTE_PATTERNS = (
    re.compile(r"authorization", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    # Match an auth token, not token *counts* (input_tokens/output_tokens).
    re.compile(r"(?<![a-z])token(?!s)", re.IGNORECASE),
    re.compile(r"cookie", re.IGNORECASE),
    re.compile(r"set-cookie", re.IGNORECASE),
    re.compile(r"prompt", re.IGNORECASE),
    re.compile(r"completion", re.IGNORECASE),
    re.compile(r"request[._-]?body", re.IGNORECASE),
    re.compile(r"response[._-]?body", re.IGNORECASE),
    re.compile(r"query[._-]?string", re.IGNORECASE),
)

# Span/event names that carry user content must be replaced, not stored.
_SENSITIVE_NAME_PATTERN = re.compile(
    r"prompt|completion|request body|response body|authorization|cookie",
    re.IGNORECASE,
)

REDACTED_NAME = "<redacted>"
REDACTED_EVENT = "<redacted-event>"
REDACTED_STATUS = "<redacted>"

# OTLP JSON uses these field names for 64-bit numbers; kept as strings.
_ATTR_KEY = "key"
_ATTR_VALUE = "value"


def _is_sensitive_key(key: str) -> bool:
    return any(pattern.search(key) for pattern in SENSITIVE_ATTRIBUTE_PATTERNS)


def is_allowed_attribute_key(key: str, allowlist: frozenset[str]) -> bool:
    """Return whether an attribute key survives sanitization."""
    if not isinstance(key, str) or not key:
        return False
    if _is_sensitive_key(key):
        return False
    if key in allowlist:
        return True
    return any(pattern.match(key) for pattern in SPAN_ATTRIBUTE_PATTERNS)


def _redact_url_credentials(value: Any) -> Any:
    """Drop userinfo and query strings from a URL-like string."""
    if not isinstance(value, str):
        return value
    prefix, sep, rest = value.partition("://")
    if not sep:
        return value
    if "@" not in rest:
        # Keep the host but drop any query string.
        host = rest.split("?", 1)[0]
        return f"{prefix}{sep}{host}"
    _, _, host = rest.partition("@")
    host = host.split("?", 1)[0]
    return f"{prefix}{sep}<redacted>@{host}"


_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}"),
)


def redact_value(value: Any) -> Any:
    """Redact obviously sensitive content inside an otherwise allowed value."""
    if not isinstance(value, str):
        return value
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        if pattern.search(value):
            return REDACTED_STATUS
    return _redact_url_credentials(value)


def _sanitize_attributes(
    attributes: list[dict[str, Any]],
    allowlist: frozenset[str],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for attr in attributes or []:
        if not isinstance(attr, dict):
            continue
        key = attr.get(_ATTR_KEY)
        if not is_allowed_attribute_key(key, allowlist):
            continue
        sanitized.append({_ATTR_KEY: key, _ATTR_VALUE: redact_value(attr.get(_ATTR_VALUE))})
    return sanitized


def _sanitize_name(name: Any) -> str:
    if not isinstance(name, str) or _SENSITIVE_NAME_PATTERN.search(name):
        return REDACTED_NAME
    return name


def sanitize_span(span: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized copy of one OTLP JSON span."""
    out = dict(span)
    out["name"] = _sanitize_name(span.get("name"))
    out["attributes"] = _sanitize_attributes(span.get("attributes"), SPAN_ATTRIBUTE_ALLOWLIST)

    status = span.get("status")
    if isinstance(status, dict):
        # Keep the numeric code; drop the free-form description entirely.
        out["status"] = {"code": status.get("code", 0)}
    else:
        out["status"] = {"code": 0}

    events = []
    for event in span.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_out = dict(event)
        event_out["name"] = _sanitize_event_name(event.get("name"))
        event_out["attributes"] = _sanitize_attributes(
            event.get("attributes"), SPAN_ATTRIBUTE_ALLOWLIST
        )
        events.append(event_out)
    out["events"] = events
    return out


def _sanitize_event_name(name: Any) -> str:
    if not isinstance(name, str) or _SENSITIVE_NAME_PATTERN.search(name):
        return REDACTED_EVENT
    return name


def sanitize_resource(resource: dict[str, Any]) -> dict[str, Any]:
    """Sanitize one OTLP JSON resource block."""
    out = dict(resource)
    out["attributes"] = _sanitize_attributes(
        resource.get("attributes"), RESOURCE_ATTRIBUTE_ALLOWLIST
    )
    return out


def sanitize_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """Keep scope name/version (instrumentation identity), drop free-form attrs."""
    out = dict(scope)
    out["attributes"] = []
    return out


def sanitize_export_request(request: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a decoded OTLP JSON export request (``resourceSpans`` tree).

    Returns a new dict; the input is not mutated.
    """
    out = dict(request)
    resource_spans: list[dict[str, Any]] = []
    for resource_span in request.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        rs_out = dict(resource_span)
        rs_out["resource"] = sanitize_resource(resource_span.get("resource") or {})
        scopes = []
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, dict):
                continue
            ss_out = dict(scope_span)
            ss_out["scope"] = sanitize_scope(scope_span.get("scope") or {})
            ss_out["spans"] = [sanitize_span(span) for span in scope_span.get("spans") or []]
            scopes.append(ss_out)
        rs_out["scopeSpans"] = scopes
        resource_spans.append(rs_out)
    out["resourceSpans"] = resource_spans
    return out


__all__ = [
    "SANITIZATION_POLICY",
    "SANITIZATION_POLICY_VERSION",
    "RESOURCE_ATTRIBUTE_ALLOWLIST",
    "SPAN_ATTRIBUTE_ALLOWLIST",
    "is_allowed_attribute_key",
    "redact_value",
    "sanitize_export_request",
    "sanitize_resource",
    "sanitize_scope",
    "sanitize_span",
]
