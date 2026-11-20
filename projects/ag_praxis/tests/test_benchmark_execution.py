"""Tests for the ag_praxis benchmark execution plumbing into the guidellm toolbox."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_validated_replica_count_valid() -> None:
    from projects.ag_praxis.orchestration import test_phase

    assert test_phase._validated_replica_count({"replica_count": 4}) == 4


def test_validated_replica_count_none_raises() -> None:
    from projects.ag_praxis.orchestration import test_phase

    with pytest.raises(ValueError, match="replica_count"):
        test_phase._validated_replica_count({"replica_count": None})


def test_validated_replica_count_missing_raises() -> None:
    from projects.ag_praxis.orchestration import test_phase

    with pytest.raises(ValueError, match="replica_count"):
        test_phase._validated_replica_count({})


def test_validated_replica_count_zero_raises() -> None:
    from projects.ag_praxis.orchestration import test_phase

    with pytest.raises(ValueError, match="replica_count"):
        test_phase._validated_replica_count({"replica_count": 0})


def test_validated_replica_count_negative_raises() -> None:
    from projects.ag_praxis.orchestration import test_phase

    with pytest.raises(ValueError, match="replica_count"):
        test_phase._validated_replica_count({"replica_count": -2})


def test_execute_benchmark_rejects_invalid_replica_count() -> None:
    from projects.ag_praxis.orchestration import test_phase

    benchmark = {"job_name": "guidellm", "rate": "4", "replica_count": 0}
    with pytest.raises(ValueError, match="replica_count"):
        test_phase._execute_benchmark("test-namespace", "http://endpoint", benchmark, "smoke")


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

    benchmark = {"job_name": "guidellm", "rate": "4", "replica_count": 1}
    test_phase._execute_benchmark("test-namespace", "http://endpoint", benchmark, "smoke")

    assert captured["drop_request_details"] is True
    assert captured["keep_full_benchmark_file"] is False
