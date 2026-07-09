"""Minimal Dash read-only KPI table (FR-010 baseline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_layout(kpi_json_path: Path) -> Any:
    """Return Dash layout from a KPI JSON file (no server run here)."""
    from dash import html  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    if kpi_json_path.is_file():
        for line in kpi_json_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))

    table = html.Table(
        [html.Tr([html.Th("kpi_id"), html.Th("value"), html.Th("run_id")])]
        + [
            html.Tr(
                [
                    html.Td(r.get("kpi_id", "")),
                    html.Td(str(r.get("value", ""))),
                    html.Td(r.get("run_id", "")),
                ]
            )
            for r in rows[:500]
        ]
    )
    return html.Div([html.H3("Caliper — KPI snapshot"), table])


def make_app(kpi_json_path: Path) -> Any:
    """Construct a Dash app for local use."""
    from dash import Dash  # noqa: PLC0415

    app = Dash(__name__, title="FORGE KPI view")
    app.layout = build_layout(kpi_json_path)
    return app
