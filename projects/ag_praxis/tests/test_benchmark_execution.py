"""Tests for the ag_praxis benchmark execution plumbing into the guidellm toolbox."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_resolve_replica_scaling_derived_exact() -> None:
    from projects.ag_praxis.orchestration import test_phase

    benchmark = {"replica_count": None, "users_per_replica": 64}
    assert test_phase._resolve_replica_scaling(128, benchmark) == (2, 64)


def test_resolve_replica_scaling_derived_non_even_rate() -> None:
    from projects.ag_praxis.orchestration import test_phase

    benchmark = {"replica_count": None, "users_per_replica": 64}
    replicas, per_replica_rate = test_phase._resolve_replica_scaling(100, benchmark)
    assert replicas == 2
    assert per_replica_rate == 50
    assert replicas * per_replica_rate == 100


def test_resolve_replica_scaling_derived_single_replica() -> None:
    from projects.ag_praxis.orchestration import test_phase

    benchmark = {"replica_count": None, "users_per_replica": 64}
    assert test_phase._resolve_replica_scaling(8, benchmark) == (1, 8)


def test_resolve_replica_scaling_explicit_override_wins() -> None:
    from projects.ag_praxis.orchestration import test_phase

    benchmark = {"replica_count": 4, "users_per_replica": 64}
    assert test_phase._resolve_replica_scaling(128, benchmark) == (4, 32)


def test_resolve_replica_scaling_missing_users_per_replica_raises() -> None:
    from projects.ag_praxis.orchestration import test_phase

    benchmark = {"replica_count": None}
    with pytest.raises(ValueError, match="users_per_replica"):
        test_phase._resolve_replica_scaling(64, benchmark)


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
