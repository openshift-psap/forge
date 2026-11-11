"""Tests for the ag_praxis benchmark execution plumbing into the guidellm toolbox."""

from __future__ import annotations

from types import SimpleNamespace


def test_execute_benchmark_passes_retention_flags(monkeypatch) -> None:
    from projects.ag_praxis.orchestration import test_phase
    from projects.core.library import config
    from projects.guidellm.toolbox.run_guidellm_benchmark import main as guidellm_main

    config_values = {
        "workloads.validate_backend": False,
        "workloads.keep_full_benchmark_file": False,
        "workloads.drop_request_details": True,
    }

    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda key, default=None: config_values.get(key, default)),
    )

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(guidellm_main, "run", fake_run)

    benchmark = {"job_name": "guidellm", "rate": "4"}
    test_phase._execute_benchmark("test-namespace", "http://endpoint", benchmark, "smoke")

    assert captured["drop_request_details"] is True
    assert captured["keep_full_benchmark_file"] is False
