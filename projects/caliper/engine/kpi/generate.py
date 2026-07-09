"""Emit canonical KPI JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from projects.caliper.engine.parse import run_parse
from projects.caliper.engine.validation import load_schema, schema_path, validate_instance


def run_kpi_generate(
    *,
    base_dir: Path,
    plugin_module: str,
    plugin: object,
    output: Path | None,
    use_cache: bool,
    cache_path: Path | None,
) -> list[dict[str, Any]]:
    model = run_parse(
        base_dir=base_dir,
        plugin_module=plugin_module,
        plugin=plugin,
        use_cache=use_cache,
    )
    compute = plugin.compute_kpis
    rows: list[dict[str, Any]] = compute(model)
    kpi_schema = load_schema(schema_path("kpi_record.schema.json"))
    for row in rows:
        validate_instance(row, kpi_schema, "KPI record")
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")
    if output:
        output.write_text(text, encoding="utf-8")
    return rows
