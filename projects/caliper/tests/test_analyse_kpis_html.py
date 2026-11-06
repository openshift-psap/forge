"""Regression test for the `caliper kpi analyse-kpis --html` gating bug.

The HTML regression report must be generated whenever the analysis actually
ran and wrote a JSON report, including when a regression was detected
(`status_data.success=False`, `status_data.output_file` set). It must NOT be
attempted when analysis never ran (e.g. invalid input, `output_file=None`).
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from click.testing import CliRunner

import projects.caliper.cli.commands as commands_mod
from projects.caliper.public.status_models import KpiAnalysisStatus, StatusLevel


def _status(*, success: bool, output_file: str | None, status: StatusLevel) -> KpiAnalysisStatus:
    return KpiAnalysisStatus(
        status=status,
        completed_at=time.time(),
        success=success,
        output_file=output_file,
        regressions_detected=not success,
    )


def _invoke(tmp_path: Path, monkeypatch, status_data: KpiAnalysisStatus):
    current = tmp_path / "kpis.json"
    current.write_text("{}", encoding="utf-8")
    historical = tmp_path / "historical"
    historical.mkdir()
    output = tmp_path / "regression_analyze" / "kpi_analyze.json"

    monkeypatch.setattr(
        "projects.caliper.engine.kpi.analyze.analyze_kpis",
        MagicMock(return_value=(status_data, {})),
    )
    generate_mock = MagicMock()
    monkeypatch.setattr(
        "projects.caliper.engine.kpi.html_generator.generate_regression_html_from_file",
        generate_mock,
    )

    runner = CliRunner()
    result = runner.invoke(
        commands_mod.analyse_kpis_cmd,
        [
            "--output",
            str(output),
            "--current-kpis-file",
            str(current),
            "--historical-kpis-dir",
            str(historical),
            "--plugin",
            "some.plugin.module",
            "--html",
        ],
    )
    return result, generate_mock


def test_html_generated_when_regression_detected(tmp_path: Path, monkeypatch):
    """A detected regression sets success=False but must still produce HTML."""
    status_data = _status(
        success=False,
        output_file=str(tmp_path / "regression_analyze" / "kpi_analyze.json"),
        status=StatusLevel.REGRESSION_DETECTED,
    )

    result, generate_mock = _invoke(tmp_path, monkeypatch, status_data)

    generate_mock.assert_called_once()
    assert "Generated HTML regression report" in result.output


def test_html_generated_on_success(tmp_path: Path, monkeypatch):
    status_data = _status(
        success=True,
        output_file=str(tmp_path / "regression_analyze" / "kpi_analyze.json"),
        status=StatusLevel.SUCCESS,
    )

    result, generate_mock = _invoke(tmp_path, monkeypatch, status_data)

    generate_mock.assert_called_once()
    assert "Generated HTML regression report" in result.output


def test_html_skipped_when_no_report_was_written(tmp_path: Path, monkeypatch):
    """If analysis bailed out before writing a report, there's nothing to render."""
    status_data = _status(success=False, output_file=None, status=StatusLevel.FAILED)

    result, generate_mock = _invoke(tmp_path, monkeypatch, status_data)

    generate_mock.assert_not_called()
    assert "Generated HTML regression report" not in result.output
