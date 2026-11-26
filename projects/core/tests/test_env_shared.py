"""Initialization of Forge's optional private workspace shared across steps."""

from pathlib import Path

from projects.core.library import env


def test_shared_workspace_is_optional_and_does_not_reuse_previous_value(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGE_SHARED_DIR", raising=False)
    monkeypatch.setattr(env, "FORGE_SHARED_DIR", tmp_path / "previous-job")

    env.init()

    assert env.FORGE_SHARED_DIR is None
    assert not (tmp_path / "previous-job").exists()


def test_shared_workspace_is_created_once_and_survives_artifact_directory_changes(
    monkeypatch, tmp_path
):
    shared = tmp_path / "private" / "shared"
    monkeypatch.setenv("FORGE_SHARED_DIR", str(shared))
    monkeypatch.setattr(env, "FORGE_SHARED_DIR", None)

    env.init()

    assert isinstance(env.FORGE_SHARED_DIR, Path)
    assert env.FORGE_SHARED_DIR == shared.resolve()
    assert shared.is_dir()
    assert shared.stat().st_mode & 0o777 == 0o700
    marker = shared / "saved-state"
    marker.write_text("test-state")
    with env.TempArtifactDir(env.ARTIFACT_DIR / "next-task"):
        assert env.FORGE_SHARED_DIR == shared.resolve()
    env.init()
    assert marker.read_text() == "test-state"
