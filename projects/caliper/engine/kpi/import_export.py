"""KPI import from / export to OpenSearch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi.opensearch_client import build_client


def export_kpis_to_index(
    records: list[dict[str, Any]],
    *,
    index: str | None = None,
) -> None:
    client = build_client()
    idx = index or os.environ.get("OPENSEARCH_KPI_INDEX", "forge-kpis")
    for rec in records:
        # Use kpi_id + run_id + timestamp as id
        doc_id = f"{rec.get('kpi_id')}-{rec.get('run_id')}-{rec.get('timestamp')}"
        client.index(index=idx, id=doc_id, body=rec, refresh=True)


def import_kpis_snapshot(
    *,
    snapshot_path: Path,
    index: str | None = None,
    max_hits: int = 10_000,
) -> list[dict[str, Any]]:
    """Download KPIs from OpenSearch into snapshot file."""
    client = build_client()
    idx = index or os.environ.get("OPENSEARCH_KPI_INDEX", "forge-kpis")
    body = {"size": max_hits, "query": {"match_all": {}}}
    resp = client.search(index=idx, body=body)
    hits = [h["_source"] for h in resp.get("hits", {}).get("hits", [])]
    snapshot_path.write_text(
        "\n".join(json.dumps(h, ensure_ascii=False) for h in hits) + ("\n" if hits else ""),
        encoding="utf-8",
    )
    return hits


def load_kpis_json(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))
