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
        )
