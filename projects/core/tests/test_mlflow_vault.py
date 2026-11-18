from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.file_export.mlflow_secrets import load_mlflow_secrets_yaml
from projects.core.library import config, env


@pytest.mark.parametrize(
    "project_name",
    ["ag_praxis", "inference_playbooks", "llm_d", "mcp_gateway", "minimal", "rhaiis", "skeleton"],
)
@pytest.mark.parametrize("override", [None, "staging"])
def test_mlflow_instance_selects_profile(monkeypatch, tmp_path, project_name, override):
    monkeypatch.setattr(config, "project", None)
    monkeypatch.setenv("FORGE_MLFLOW_INSTANCE", "previous")
    orchestration_dir = Path(env.FORGE_HOME) / "projects" / project_name / "orchestration"
    if override is not None:
        config.write_variables_override(
            variables_dict={"caliper.export.backend.mlflow.instance": override}
        )

    config.init(orchestration_dir, apply_cluster_config=False)

    vault = config.project.get_config("caliper.export.backend.mlflow.secrets.vault", print=False)
    assert vault == {"name": "psap-forge-mlflow-export", "mlflow_secret": "mlflow-secret.yaml"}
    path = tmp_path / vault["mlflow_secret"]
    profiles = {
        "prod": {"tracking_uri": "https://prod.example", "token": "fake-prod"},
        "staging": {"tracking_uri": "https://staging.example", "token": "fake-staging"},
    }
    path.write_text(yaml.safe_dump(profiles))
    assert load_mlflow_secrets_yaml(path) == profiles[override or "prod"]


def test_mlflow_profile_names_come_from_file(monkeypatch, tmp_path):
    path = tmp_path / "mlflow-secret.yaml"
    profile = {"tracking_uri": "https://qa.example", "token": "fake-qa"}
    path.write_text(yaml.safe_dump({"qa": profile}))
    monkeypatch.setenv("FORGE_MLFLOW_INSTANCE", "qa")
    assert load_mlflow_secrets_yaml(path) == profile


@pytest.mark.parametrize("profiled", [False, True])
def test_missing_staging_profile_cannot_fall_back_to_production(monkeypatch, tmp_path, profiled):
    path = tmp_path / "mlflow-secret.yaml"
    prod = {"tracking_uri": "https://prod.example", "token": "fake-prod"}
    path.write_text(yaml.safe_dump({"prod": prod} if profiled else prod))

    monkeypatch.delenv("FORGE_MLFLOW_INSTANCE", raising=False)
    assert load_mlflow_secrets_yaml(path) == prod
    monkeypatch.setenv("FORGE_MLFLOW_INSTANCE", "staging")
    with pytest.raises(ValueError, match="profile 'staging' is missing"):
        load_mlflow_secrets_yaml(path)
