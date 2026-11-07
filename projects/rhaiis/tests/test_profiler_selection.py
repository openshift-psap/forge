from types import SimpleNamespace

import pytest

from projects.core.library import config
from projects.guidellm.toolbox.run_guidellm_benchmark import main as guidellm
from projects.rhaiis.orchestration.test_phase import (
    _optional_phase_workload_keys,
    _run_warmup_step,
)


def test_configured_workloads_are_excluded_from_optional_phases() -> None:
    assert _optional_phase_workload_keys(["profile1", "profile5", "profile7"], ["profile5"]) == [
        "profile1",
        "profile7",
    ]


def test_all_configured_exclusions_can_leave_no_optional_phase_workloads() -> None:
    assert _optional_phase_workload_keys(["profile5"], ["profile5"]) == []


def test_exclusions_are_configurable_and_empty_by_default() -> None:
    assert _optional_phase_workload_keys(["profile1", "profile5", "profile7"], []) == [
        "profile1",
        "profile5",
        "profile7",
    ]


def test_zero_workload_warmup_skips_priming_run(monkeypatch) -> None:
    monkeypatch.setattr(config, "project", SimpleNamespace(get_config=lambda *_args, **_kwargs: {}))
    monkeypatch.setattr(
        guidellm, "run", lambda **kwargs: pytest.fail("warmup run should be skipped")
    )

    _run_warmup_step(
        deployment_name="model",
        namespace="namespace",
        endpoint_url="http://model:8000",
        benchmark_cfg={},
        model_cfg={"hf_model_id": "org/model"},
        workload={"warmup": 0, "data": "prompt_tokens=1,output_tokens=1"},
        workload_key="profile1",
        benchmark_timeout=60,
    )
