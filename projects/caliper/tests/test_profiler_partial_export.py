from projects.caliper.engine.file_export.runner import apply_redaction_policy


def test_incomplete_profiler_trace_is_never_exported(tmp_path) -> None:
    partial = tmp_path / "trace_rank0_pid1_runprofile1_range500-503.json.partial"
    complete = tmp_path / "trace_rank0_pid1_runprofile1_range500-503.json"
    partial.write_bytes(b"incomplete")
    complete.write_bytes(b"complete")

    assert apply_redaction_policy([partial, complete], None) == [complete]
