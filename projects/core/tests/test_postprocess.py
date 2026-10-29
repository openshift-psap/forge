from projects.core.library import env, postprocess


def test_run_and_postprocess_skips_postprocessing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(env, "ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(
        postprocess,
        "run_postprocess_after_test",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("postprocessing must not run when disabled")
        ),
    )

    result = postprocess.run_and_postprocess(lambda **kwargs: 0, run_postprocess=False)

    assert result == 0


def test_run_and_postprocess_runs_postprocessing_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(env, "ARTIFACT_DIR", str(tmp_path))
    calls = []

    def fake_postprocess(artifact_dir, *, test_outcome=None):
        calls.append(artifact_dir)
        return {"success": True, "final_status": "ok"}

    monkeypatch.setattr(postprocess, "run_postprocess_after_test", fake_postprocess)

    result = postprocess.run_and_postprocess(lambda **kwargs: 0)

    assert result == 0
    assert len(calls) == 1
