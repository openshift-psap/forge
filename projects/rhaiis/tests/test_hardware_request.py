"""Tests for the RHAIIS Fournos hardware request resolver."""

import types

import ci
import pytest

from projects.core.library import config as core_config


@pytest.fixture
def resolver_env(monkeypatch):
    """Stub the runtime config so the resolver can run without a cluster.

    ``configure`` returns a dict recording the arguments the resolver passes
    to the engine-args merge, so tests can assert which workload it selected.
    """

    def configure(
        tp_size=1,
        replicas=1,
        gpu_type="h200",
        workload=None,
        workload_keys=None,
        merge_result=None,
    ):
        captured = {}
        workload = workload if workload is not None else {}
        merge_result = (
            merge_result if merge_result is not None else {"tensor-parallel-size": tp_size}
        )

        def get_workload(key):
            captured["workload_key"] = key
            return workload

        def merge_engine_args(engine_defaults, model, merged_workload, engine):
            captured["workload"] = merged_workload
            captured["engine_defaults"] = engine_defaults
            return merge_result

        def get_config(key, default=None):
            if key == "tests.rhaiis.workload_keys":
                return workload_keys if workload_keys is not None else []
            return default if default is not None else {}

        monkeypatch.setattr(ci.test_rhaiis, "init", lambda: None)
        monkeypatch.setattr(ci.runtime_config, "get_test_model_key", lambda: "model")
        monkeypatch.setattr(ci.runtime_config, "get_model", lambda key: {})
        monkeypatch.setattr(ci.runtime_config, "get_engine", lambda: "vllm")
        monkeypatch.setattr(ci.runtime_config, "get_engine_args", lambda engine: {})
        monkeypatch.setattr(ci.runtime_config, "get_test_workload_key", lambda: "wl-default")
        monkeypatch.setattr(ci.runtime_config, "get_workload", get_workload)
        monkeypatch.setattr(ci.runtime_config, "merge_engine_args", merge_engine_args)
        monkeypatch.setattr(ci.runtime_config, "get_deploy_config", lambda: {"replicas": replicas})
        monkeypatch.setattr(ci.runtime_config, "get_accelerator", lambda: "h200")
        monkeypatch.setattr(ci.runtime_config, "get_gpu_type", lambda accelerator: gpu_type)
        monkeypatch.setattr(
            core_config,
            "project",
            types.SimpleNamespace(get_config=get_config),
        )
        return captured

    return configure


def test_infers_gpu_type_and_replica_shape(resolver_env):
    resolver_env(tp_size=4, replicas=3)

    hardware = ci.resolve_hardware_request({})

    assert hardware == {
        "gpuCount": 4,
        "gpuType": "h200",
        "replicas": 3,
        "gpusPerReplica": 4,
    }


def test_keeps_explicit_hardware_and_adds_shape(resolver_env):
    resolver_env(tp_size=4, replicas=2)

    hardware = ci.resolve_hardware_request({"gpuType": "a100", "gpuCount": 8})

    assert hardware["gpuType"] == "a100"
    assert hardware["gpuCount"] == 8
    assert hardware["replicas"] == 2
    assert hardware["gpusPerReplica"] == 4


def test_explicit_shape_is_not_overwritten(resolver_env):
    resolver_env(tp_size=4, replicas=2)

    hardware = ci.resolve_hardware_request({"replicas": 5, "gpusPerReplica": 1})

    assert hardware["replicas"] == 5
    assert hardware["gpusPerReplica"] == 1


def test_defaults_to_one_replica_and_one_gpu(resolver_env):
    resolver_env()

    hardware = ci.resolve_hardware_request({})

    assert hardware["replicas"] == 1
    assert hardware["gpusPerReplica"] == 1


def test_unknown_gpu_type_returns_empty(resolver_env):
    resolver_env(tp_size=4, replicas=2, gpu_type=None)

    assert ci.resolve_hardware_request({}) == {}


def test_merges_the_workload_the_test_phase_deploys(resolver_env):
    workload = {"vllm_args": {"tensor-parallel-size": 8}}
    captured = resolver_env(
        tp_size=4,
        workload=workload,
        merge_result={"tensor-parallel-size": 8},
    )

    hardware = ci.resolve_hardware_request({})

    # The deployment merges the first selected workload; the resolver must
    # report the same TP size or the shape check validates the wrong pod.
    assert captured["workload"] == workload
    assert hardware["gpusPerReplica"] == 8


def test_prefers_the_first_configured_workload_key(resolver_env):
    captured = resolver_env(workload_keys=["wl-b", "wl-c"])

    ci.resolve_hardware_request({})

    assert captured["workload_key"] == "wl-b"


def test_defaults_to_the_configured_test_workload(resolver_env):
    captured = resolver_env()

    ci.resolve_hardware_request({})

    assert captured["workload_key"] == "wl-default"


def test_zero_replicas_is_reported_as_zero(resolver_env):
    resolver_env(replicas=0)

    assert ci.resolve_hardware_request({})["replicas"] == 0
