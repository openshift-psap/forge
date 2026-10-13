from projects.rhaiis.orchestration.test_phase import (
    _optional_phase_workload_keys,
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
