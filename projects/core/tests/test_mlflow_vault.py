from pathlib import Path

import pytest
import yaml

from projects.core.library import config, env


@pytest.mark.parametrize(
    "project_name",
    ["ag_praxis", "inference_playbooks", "llm_d", "mcp_gateway", "minimal", "rhaiis", "skeleton"],
)
@pytest.mark.parametrize(
    ("override", "filename"),
    [(None, "mlflow-secret.yaml"), ("staging", "mlflow-staging-secret.yaml")],
)
def test_mlflow_instance_selects_file_in_same_vault(monkeypatch, project_name, override, filename):
    monkeypatch.setattr(config, "project", None)
    orchestration_dir = Path(env.FORGE_HOME) / "projects" / project_name / "orchestration"
    if override is not None:
        config.write_variables_override(variables_dict={"mlflow_instance": override})

    config.init(orchestration_dir, apply_cluster_config=False)

    vault = config.project.get_config("caliper.export.backend.mlflow.secrets.vault", print=False)
    assert vault == {"name": "psap-forge-mlflow-export", "mlflow_secret": filename}
    definition = Path(env.FORGE_HOME) / "vaults" / f"{vault['name']}.yaml"
    assert filename in yaml.safe_load(definition.read_text())["content"]


def test_mlflow_instance_rejects_unknown_value(monkeypatch):
    monkeypatch.setattr(config, "project", None)
    config.write_variables_override(variables_dict={"mlflow_instance": "qa"})

    with pytest.raises(ValueError, match="mlflow_instance must be"):
        config.init(Path(env.FORGE_HOME) / "projects/llm_d/orchestration", apply_cluster_config=False)
