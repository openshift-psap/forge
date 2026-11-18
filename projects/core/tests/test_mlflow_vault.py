from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.file_export.mlflow_secrets import load_mlflow_secrets_yaml
from projects.caliper.orchestration.mlflow_credentials import (
    INSTANCE_CONFIG_PATH,
    INSTANCE_SECRET_KEY,
    load_mlflow_connection,
    resolve_mlflow_credentials,
)
from projects.core.library import config, env, vault

VAULT_NAME = "psap-forge-mlflow-export"
DEFAULT_KEY = "mlflow-secret.yaml"
PROD = {"tracking_uri": "https://prod.example", "token": "fake-prod"}
STAGING = {"tracking_uri": "https://staging.example", "token": "fake-staging"}


@pytest.mark.parametrize(
    "project_name",
    ["ag_praxis", "inference_playbooks", "llm_d", "mcp_gateway", "minimal", "rhaiis", "skeleton"],
)
@pytest.mark.parametrize("override", [None, "staging"])
def test_mlflow_instance_selects_explicit_file_and_profile(
    monkeypatch, tmp_path, project_name, override
):
    monkeypatch.setattr(config, "project", None)
    orchestration_dir = Path(env.FORGE_HOME) / "projects" / project_name / "orchestration"
    if override is not None:
        config.write_variables_override(variables_dict={INSTANCE_CONFIG_PATH: override})

    config.init(orchestration_dir, apply_cluster_config=False)

    vault_ref = config.project.get_config(
        "caliper.export.backend.mlflow.secrets.vault", print=False
    )
    assert vault_ref == {"name": VAULT_NAME, "mlflow_secret": DEFAULT_KEY}
    (tmp_path / DEFAULT_KEY).write_text(yaml.safe_dump(PROD))
    if override:
        (tmp_path / INSTANCE_SECRET_KEY).write_text(yaml.safe_dump({"staging": STAGING}))
    monkeypatch.setattr(vault, "get_vault_content_path", lambda name, key: tmp_path / key)

    path, instance = resolve_mlflow_credentials(vault_ref["name"], vault_ref["mlflow_secret"])

    assert path.name == (INSTANCE_SECRET_KEY if override else DEFAULT_KEY)
    assert instance == override
    assert load_mlflow_connection(vault_ref["name"], vault_ref["mlflow_secret"]) == (
        STAGING if override else PROD
    )


def test_existing_vault_validates_without_instance_file(monkeypatch, tmp_path):
    secret_dir = tmp_path / "mounted"
    secret_dir.mkdir()
    for filename in (DEFAULT_KEY, "tracking_uri", "username", "password"):
        (secret_dir / filename).write_text("fake")
    monkeypatch.setenv("PSAP_FORGE_MLFLOW_EXPORT_SECRET_PATH", str(secret_dir))

    manager = vault.VaultManager(Path(env.FORGE_HOME) / "vaults")
    assert manager.validate_vault(VAULT_NAME, strict=True)

    (secret_dir / DEFAULT_KEY).unlink()
    assert not manager.validate_vault(VAULT_NAME, strict=True)


def test_explicit_instance_requires_instance_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "project", None)
    config.write_variables_override(variables_dict={INSTANCE_CONFIG_PATH: "staging"})
    config.init(Path(env.FORGE_HOME) / "projects/llm_d/orchestration", apply_cluster_config=False)
    (tmp_path / DEFAULT_KEY).write_text(yaml.safe_dump(PROD))
    monkeypatch.setattr(vault, "get_vault_content_path", lambda name, key: tmp_path / key)

    with pytest.raises(FileNotFoundError, match=INSTANCE_SECRET_KEY):
        load_mlflow_connection(VAULT_NAME, DEFAULT_KEY)


def test_mlflow_profile_names_come_from_file(tmp_path):
    path = tmp_path / INSTANCE_SECRET_KEY
    qa = {"tracking_uri": "https://qa.example", "token": "fake-qa"}
    path.write_text(yaml.safe_dump({"qa": qa, "prod": PROD}))

    assert load_mlflow_secrets_yaml(path, mlflow_instance="qa") == qa
    with pytest.raises(ValueError, match="profile 'staging' is missing"):
        load_mlflow_secrets_yaml(path, mlflow_instance="staging")
