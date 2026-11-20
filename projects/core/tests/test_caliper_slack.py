"""Tests for CaliperSlackProvider and analyze-report Slack formatting."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

# Provider imports slack_sdk at module load; stub only when the package is absent.
try:
    import slack_sdk  # noqa: F401
except ImportError:
    _slack = ModuleType("slack_sdk")
    _slack.WebClient = MagicMock  # type: ignore[attr-defined]
    _slack.errors = ModuleType("slack_sdk.errors")
    _slack.errors.SlackApiError = type("SlackApiError", (Exception,), {})  # type: ignore[attr-defined]
    sys.modules["slack_sdk"] = _slack
    sys.modules["slack_sdk.errors"] = _slack.errors

import pytest

from projects.core.notifications.caliper_slack import (
    CaliperSlackProvider,
    format_caliper_slack_message,
    format_interesting_changes,
    interesting_results,
    load_analyze_report,
)
from projects.core.notifications.provider import NotificationContext


def _result_row(row: dict, verdict: str) -> dict:
    """Build a result row matching the real (nested) analyze engine schema.

    ``current_value`` is a ``{"value": x, "comparison_keys": {...}}`` object
    and ``baseline_mean``/``relative_change`` live under ``details``, not at
    the top level. ``relative_change`` is a fraction (e.g. -0.083 for -8.3%).
    """
    return {
        "kpi_id": row["kpi_id"],
        "labels": row.get("labels", {"num_servers": "1", "users": "16", "target": "gateway"}),
        "current_value": {"value": row["current"], "comparison_keys": {}},
        "higher_is_better": row.get("higher_is_better", True),
        "verdict": verdict,
        "baseline_values": {"mcp_gateway_version=0.6.2": row["baseline"]},
        "details": {
            "algorithm": "SCALAR_RELATIVE_CHANGE",
            "baseline_mean": row["baseline"],
            "relative_change": row["pct"] / 100.0,
        },
    }


def _report(*, regressions: list[dict] | None = None, improvements: list[dict] | None = None):
    results = []
    for row in regressions or []:
        results.append(_result_row(row, "REGRESSION"))
    for row in improvements or []:
        results.append(_result_row(row, "PASS"))
    return {
        "analysis": {
            "status": "REGRESSION_DETECTED" if regressions else "PASS",
            "config": {
                "comparison_keys": ["mcp_gateway_version"],
                "max_relative_regression": 0.1,
            },
        },
        "results": results,
        "overall": {
            "verdict": "REGRESSION_DETECTED" if regressions else "PASS",
            "regression_count": len(regressions or []),
            "total_tested": len(results),
            "total_skipped": 0,
        },
    }


def _write_report(tmp_path: Path, payload: dict) -> Path:
    out = tmp_path / "regression_analyze" / "kpi_analyze.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


class _FakeProject:
    def __init__(self, tmp_path: Path | None = None, *, notify_always: bool = False):
        self._tmp = tmp_path
        self._notify_always = notify_always

    def get_config(self, key, default=None, print=False, warn=False):
        if key == "notifications.slack.channel_id":
            return "C0BEBS6929L"
        if key == "notifications.slack.notify_always":
            return self._notify_always
        if key == "caliper.postprocess.analyze.output":
            return "regression_analyze/kpi_analyze.json"
        if key == "caliper.export.from" and self._tmp is not None:
            return str(self._tmp)
        return default


@pytest.fixture
def fake_config(monkeypatch):
    def _install(tmp_path: Path | None = None, *, notify_always: bool = False):
        project = _FakeProject(tmp_path, notify_always=notify_always)
        # Prefer the real config module (CI). Fall back only when deps like
        # jsonpath_ng are missing locally — never replace a loaded real module.
        try:
            import projects.core.library.config as config_mod
        except ModuleNotFoundError:
            config_mod = ModuleType("projects.core.library.config")
            monkeypatch.setitem(sys.modules, "projects.core.library.config", config_mod)
        monkeypatch.setattr(config_mod, "project", project, raising=False)
        return project

    return _install


def test_interesting_results_picks_regressions_and_improvements():
    report = _report(
        regressions=[
            {
                "kpi_id": "mcp_gw_tool_call_rps",
                "current": 1100,
                "baseline": 1200,
                "pct": -8.3,
                "higher_is_better": True,
            }
        ],
        improvements=[
            {
                "kpi_id": "mcp_gw_tool_call_p95_ms",
                "current": 20.0,
                "baseline": 24.0,
                "pct": -16.7,
                "higher_is_better": False,
            }
        ],
    )
    rows = interesting_results(report)
    assert len(rows) == 2
    assert {r["kpi_id"] for r in rows} == {
        "mcp_gw_tool_call_rps",
        "mcp_gw_tool_call_p95_ms",
    }


def test_format_interesting_changes_rhaiis_style():
    report = _report(
        regressions=[
            {
                "kpi_id": "mcp_gw_tool_call_rps",
                "current": 1100,
                "baseline": 1200,
                "pct": -8.3,
            }
        ]
    )
    text = format_interesting_changes(interesting_results(report))
    assert ":red_circle:" in text
    assert "tool call rps" in text
    assert "dropped 8.3%" in text
    assert "num_servers=1" in text


def test_load_analyze_report_from_artifact_dir(tmp_path: Path, fake_config):
    fake_config(tmp_path)
    _write_report(
        tmp_path,
        _report(
            regressions=[
                {
                    "kpi_id": "mcp_gw_requests_per_second",
                    "current": 100,
                    "baseline": 200,
                    "pct": -50.0,
                }
            ]
        ),
    )
    ctx = NotificationContext(
        status={},
        finish_reason="success",
        project_name="mcp_gateway",
        artifact_dir=tmp_path,
    )
    loaded = load_analyze_report(ctx)
    assert loaded is not None
    assert loaded["overall"]["regression_count"] == 1


def test_format_message_regression_headline(tmp_path: Path, fake_config, monkeypatch):
    fake_config(tmp_path)
    monkeypatch.setenv("MCP_GATEWAY_VERSION", "0.7.0")
    _write_report(
        tmp_path,
        _report(
            regressions=[
                {
                    "kpi_id": "mcp_gw_tool_call_rps",
                    "current": 100,
                    "baseline": 200,
                    "pct": -50.0,
                }
            ]
        ),
    )
    ctx = NotificationContext(
        status={
            "caliper_artifacts_export": {
                "backends": {"mlflow": {"run_url": "https://mlflow.example/run/1"}}
            }
        },
        finish_reason="success",
        project_name="mcp_gateway",
        artifact_dir=tmp_path,
    )
    msg = format_caliper_slack_message(ctx)
    assert "Performance regressions detected" in msg
    assert "MLflow" in msg
    assert ":red_circle:" in msg
    assert "0.7.0" in msg
    assert "0.6.2" in msg


def test_provider_channel_id(fake_config):
    fake_config()
    assert CaliperSlackProvider().get_channel_id() == "C0BEBS6929L"


def test_load_analyze_report_raises_on_corrupt_json(tmp_path: Path, fake_config):
    fake_config(tmp_path)
    out = tmp_path / "regression_analyze" / "kpi_analyze.json"
    out.parent.mkdir(parents=True)
    out.write_text("{not-json", encoding="utf-8")
    ctx = NotificationContext(
        status={},
        finish_reason="success",
        project_name="mcp_gateway",
        artifact_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="not valid JSON"):
        load_analyze_report(ctx)


def test_should_notify_skips_quiet_success(tmp_path: Path, fake_config):
    fake_config(tmp_path)
    _write_report(
        tmp_path,
        {
            "analysis": {
                "status": "PASS",
                "config": {"comparison_keys": ["mcp_gateway_version"]},
            },
            "results": [
                {
                    "kpi_id": "mcp_gw_tool_call_rps",
                    "labels": {},
                    "current_value": {"value": 101, "comparison_keys": {}},
                    "higher_is_better": True,
                    "verdict": "PASS",
                    "details": {
                        "algorithm": "SCALAR_RELATIVE_CHANGE",
                        "baseline_mean": 100,
                        "relative_change": 0.01,
                    },
                }
            ],
            "overall": {
                "verdict": "PASS",
                "regression_count": 0,
                "total_tested": 1,
                "total_skipped": 0,
            },
        },
    )
    ctx = NotificationContext(
        status={},
        finish_reason="success",
        project_name="mcp_gateway",
        artifact_dir=tmp_path,
    )
    assert CaliperSlackProvider().should_notify(ctx) is False
