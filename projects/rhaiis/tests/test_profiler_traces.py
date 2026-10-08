from __future__ import annotations

from projects.rhaiis.postprocess.s3_dashboard import (
    select_rank0_profiler_traces,
    upload_profiler_traces_to_s3,
)


def test_select_rank0_profiler_traces_ignores_other_tensor_parallel_ranks(tmp_path) -> None:
    names = [
        "trace_rank0_pid100_runisl1000_osl1000_range500-503.json",
        "trace_rank0_pid101_runisl2000_osl1000_range500-503.json.gz",
        "trace_rank1_pid102_runisl1000_osl1000_range500-503.json",
        "trace_rank7_pid103_runisl1000_osl1000_range500-503.json",
        "trace_rank00_pid104_runisl1000_osl1000_range500-503.json",
    ]
    paths = []
    for name in names:
        path = tmp_path / name
        path.touch()
        paths.append(path)

    selected = select_rank0_profiler_traces(paths)

    assert [path.name for path in selected] == names[:2]


def test_trace_upload_dry_run_accepts_files_without_copying_other_ranks(tmp_path) -> None:
    rank0 = tmp_path / "trace_rank0_pid100_runisl1000_osl1000_range500-503.json"
    rank4 = tmp_path / "trace_rank4_pid104_runisl1000_osl1000_range500-503.json"
    rank0.touch()
    rank4.touch()

    result = upload_profiler_traces_to_s3(
        [rank0, rank4],
        model_name="org/model",
        accelerator="mi355x",
        tp_size=8,
        version="test",
        profile_labels=["isl1000_osl1000"],
        s3_bucket="test-bucket",
        s3_prefix="test-prefix",
        vault_name="test-vault",
        dry_run=True,
    )

    assert result == {"status": "success", "dry_run": True, "trace_count": 1}
