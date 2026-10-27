"""Prepare OTLP records for native ingestion without mutating the archive.

MLflow's server drops most resource attributes and keeps only span attributes
plus an allowlisted ``service.name``. To keep component identity visible in the
native UI, reviewed FORGE identity is copied from each record's resource
attributes into namespaced ``forge.*`` span attributes in the *payload only*.

The sanitized archive on disk is the source of record and is never modified.
"""

from __future__ import annotations

import copy
from typing import Any

# Resource attribute keys copied onto spans in the outgoing payload.
IDENTITY_KEYS = (
    "forge.job_id",
    "forge.capture_id",
    "forge.project",
    "forge.namespace",
    "forge.deployment_id",
    "forge.component.role",
)
SERVICE_NAME_KEY = "service.name"


def _resource_attributes(record: dict[str, Any], index: int) -> dict[str, Any]:
    resource_spans = record.get("resourceSpans", []) or []
    if index >= len(resource_spans):
        return {}
    resource = resource_spans[index].get("resource") or {}
    attrs: dict[str, Any] = {}
    for attr in resource.get("attributes", []) or []:
        if isinstance(attr, dict) and isinstance(attr.get("key"), str):
            attrs[attr["key"]] = attr.get("value")
    return attrs


def _span_has_attribute(span: dict[str, Any], key: str) -> bool:
    return any(
        isinstance(attr, dict) and attr.get("key") == key
        for attr in span.get("attributes", []) or []
    )


def enrich_records_for_ingestion(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deep copies of ``records`` with FORGE identity on every span.

    Existing span attributes win: identity already present on a span is not
    overwritten, so re-running is idempotent.
    """
    enriched: list[dict[str, Any]] = []
    for record in records:
        record_copy = copy.deepcopy(record)
        resource_spans = record_copy.get("resourceSpans", []) or []
        for index, resource_span in enumerate(resource_spans):
            attrs = _resource_attributes(record, index)
            additions: list[dict[str, Any]] = []
            for key in IDENTITY_KEYS:
                if key in attrs:
                    additions.append({"key": key, "value": attrs[key]})
            if not additions:
                continue
            for scope_span in resource_span.get("scopeSpans", []) or []:
                for span in scope_span.get("spans", []) or []:
                    for addition in additions:
                        if not _span_has_attribute(span, addition["key"]):
                            span.setdefault("attributes", []).append(copy.deepcopy(addition))
        enriched.append(record_copy)
    return enriched


__all__ = ["IDENTITY_KEYS", "SERVICE_NAME_KEY", "enrich_records_for_ingestion"]
