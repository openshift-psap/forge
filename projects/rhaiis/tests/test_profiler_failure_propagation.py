from types import SimpleNamespace

import pytest


def test_profiler_trace_copy_failure_propagates(monkeypatch) -> None:
    from projects.guidellm.toolbox.run_guidellm_benchmark import main as guidellm
    from projects.rhaiis.orchestration import test_phase
    from projects.rhaiis.toolbox.copy_profiler_traces import main as copy_traces
    from projects.rhaiis.toolbox.enable_profiler_gate import main as profiler_gate
    from projects.rhaiis.toolbox.verify_profiler_prereqs import main as verify_prereqs

    monkeypatch.setattr(verify_prereqs, "run", lambda **kwargs: None)
    monkeypatch.setattr(profiler_gate, "run", lambda **kwargs: None)
    monkeypatch.setattr(guidellm, "run", lambda **kwargs: None)
    monkeypatch.setattr(
        test_phase.runtime_config,
        "get_profiler_config",
        lambda: {"labels": ["profile1"]},
    )
    monkeypatch.setattr(test_phase.runtime_config, "build_guidellm_args", lambda **kwargs: [])

    def fail_copy(**kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(copy_traces, "run", fail_copy)

    with pytest.raises(RuntimeError, match="copy failed"):
        test_phase._run_profiler_step(
            deployment_name="model",
            namespace="test",
            endpoint_url="http://model",
            benchmark_cfg={},
            model_cfg={"hf_model_id": "org/model"},
            workload={"data": "prompt_tokens=1000,output_tokens=1000"},
            workload_key="profile1",
            benchmark_timeout=60,
        )


@pytest.mark.parametrize(
    "upload_result",
    [
        {"status": "failed", "error": "upload failed"},
        {"status": "success", "uploaded": 0},
    ],
)
def test_profiler_trace_upload_failure_propagates(monkeypatch, tmp_path, upload_result) -> None:
    from projects.core.library import config
    from projects.rhaiis.orchestration import test_phase
    from projects.rhaiis.postprocess import s3_dashboard

    trace_dir = tmp_path / "001__copy_profiler_traces" / "artifacts" / "traces"
    trace_dir.mkdir(parents=True)
    (trace_dir / "trace_rank0_pid100_runprofile1_range500-503.json").touch()

    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(
            get_config=lambda key, default=None: {
                "tests.rhaiis.version": "test-version",
            }.get(key, default)
        ),
    )
    monkeypatch.setattr(
        s3_dashboard,
        "upload_profiler_traces_to_s3",
        lambda *args, **kwargs: upload_result,
    )

    with pytest.raises(RuntimeError, match="Profiler trace upload"):
        test_phase._upload_profiler_traces(
            model_cfg={"hf_model_id": "org/model"},
            accelerator="mi355x",
            engine_args={"tensor-parallel-size": 8},
            profiler_cfg={"labels": ["profile1"]},
            trace_files=[trace_dir / "trace_rank0_pid100_runprofile1_range500-503.json"],
        )


def test_profiler_failure_does_not_skip_performance_benchmarks(monkeypatch, tmp_path) -> None:
    from projects.core.library import ci as ci_lib
    from projects.core.library import config
    from projects.guidellm.toolbox.run_guidellm_benchmark import main as guidellm
    from projects.rhaiis.orchestration import manifests, runtime_config, test_phase
    from projects.rhaiis.toolbox.deploy_kserve_isvc import main as deploy
    from projects.rhaiis.toolbox.wait_isvc_ready import main as wait

    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", tmp_path)
    monkeypatch.delenv("FJOB_NAME", raising=False)
    config_values = {
        "rhaiis.cluster_tag": "zeus2",
        "tests.rhaiis.version": "test-version",
        "tests.rhaiis.run_uuid": "test-run-id",
        "rhaiis.optional_phase.excluded_workloads": [],
        "tests.rhaiis.run_benchmark": True,
    }
    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(
            get_config=lambda key, default=None, **kwargs: config_values.get(key, default),
            set_config=lambda key, value: config_values.__setitem__(key, value),
        ),
    )
    monkeypatch.setattr(runtime_config, "get_model", lambda key: {"hf_model_id": "org/model"})
    monkeypatch.setattr(runtime_config, "get_accelerator", lambda: "nvidia")
    monkeypatch.setattr(runtime_config, "get_gpu_type", lambda accelerator: "h200")
    monkeypatch.setattr(runtime_config, "get_deploy_config", lambda: {})
    monkeypatch.setattr(runtime_config, "get_benchmark_config", lambda *args: {})
    monkeypatch.setattr(runtime_config, "get_engine", lambda: "vllm")
    monkeypatch.setattr(runtime_config, "get_serving_image", lambda *args: "test-image")
    monkeypatch.setattr(runtime_config, "get_engine_args", lambda engine: {})
    monkeypatch.setattr(runtime_config, "get_engine_port", lambda engine: 8000)
    monkeypatch.setattr(runtime_config, "get_workload", lambda key: {"data": "prompt_tokens=1"})
    monkeypatch.setattr(runtime_config, "merge_engine_args", lambda *args: {})
    monkeypatch.setattr(runtime_config, "merge_env_vars", lambda *args: {})
    monkeypatch.setattr(
        runtime_config,
        "get_profiler_config",
        lambda: {"enabled": True, "ranges": "500-503"},
    )
    monkeypatch.setattr(manifests, "build_servingruntime", lambda **kwargs: {})
    monkeypatch.setattr(manifests, "build_inferenceservice", lambda **kwargs: {})
    monkeypatch.setattr(deploy, "run", lambda **kwargs: None)
    monkeypatch.setattr(wait, "run", lambda **kwargs: None)
    monkeypatch.setattr(test_phase, "_capture_and_cleanup", lambda *args: None)
    monkeypatch.setattr(test_phase, "_upload_predictor_log", lambda *args: None)
    monkeypatch.setattr(test_phase, "_set_mlflow_metadata", lambda *args: None)
    notifications = []
    monkeypatch.setattr(
        ci_lib,
        "add_notification_file",
        lambda *args: notifications.append(args) or "notification.txt",
    )
    monkeypatch.setitem(guidellm.wait_guidellm_benchmark_task._retry_config, "attempts", 1)

    benchmarked = []
    uploaded = []
    trace = tmp_path / "profile1.json"
    trace.write_bytes(b"verified trace")

    def profile(*, workload_key, **kwargs):
        if workload_key == "profile4":
            raise RuntimeError("transfer failed")
        return [trace]

    monkeypatch.setattr(test_phase, "_run_profiler_step", profile)
    monkeypatch.setattr(
        test_phase,
        "_upload_profiler_traces",
        lambda *args, trace_files: uploaded.extend(trace_files) or {"uploaded": 1},
    )
    monkeypatch.setattr(
        test_phase,
        "_run_workload_benchmark",
        lambda *, workload_key, **kwargs: benchmarked.append(workload_key),
    )

    result = test_phase._run_test(
        model_key="model",
        workload_keys=["profile1", "profile4"],
        namespace="test",
        deployment_name="model",
    )

    assert result == 1
    assert uploaded == [trace]
    assert not trace.exists()
    assert benchmarked == ["profile1", "profile4"]
    assert notifications[0][0] == "profiler-profile4-failed"


def test_profiler_failure_reaches_ci_entrypoint(monkeypatch) -> None:
    from projects.core.library import ci, config
    from projects.rhaiis.orchestration import test_phase, test_rhaiis

    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda key, default=None: default),
    )
    monkeypatch.setattr(test_phase, "run", lambda **kwargs: 1)
    requested = SimpleNamespace(model_key="model", workload_key="profile4", namespace="test")

    with pytest.raises(ci.CIError, match="RHAIIS test phase failed") as error:
        test_rhaiis.test.__wrapped__(requested)

    assert error.value.category == ci.ExitCategory.TEST_FAILURE


def test_successful_rhaiis_phase_reaches_ci_entrypoint(monkeypatch) -> None:
    from projects.core.library import config
    from projects.rhaiis.orchestration import test_phase, test_rhaiis

    monkeypatch.setattr(
        config,
        "project",
        SimpleNamespace(get_config=lambda key, default=None: default),
    )
    monkeypatch.setattr(test_phase, "run", lambda **kwargs: 0)
    requested = SimpleNamespace(model_key="model", workload_key="profile1", namespace="test")

    assert test_rhaiis.test.__wrapped__(requested) == 0
