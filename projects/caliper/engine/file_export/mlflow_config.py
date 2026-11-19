"""Non-secret MLflow settings from a YAML file (separate from credentials)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.mlflow_connection import assert_tracking_uri_has_no_userinfo

# Identity / routing (not emitted as generic tags by apply_run_metadata).
MLFLOW_CONFIG_RUN_KEYS = frozenset(
    {
        "tracking_uri",
        "experiment",
        "run_name",
        "run_id",
        "workspace",
    }
)

# Applied to the run as notes and tags (see mlflow_backend._apply_run_metadata).
MLFLOW_CONFIG_METADATA_KEYS = frozenset(
    {
        "description",
        "tags",
        # Source identity (optional; git is auto-filled from artifact path when not set).
        # source_script → mlflow.source.name (e.g. my_custom_script.py); wins over source_name.
        "source_script",
        "source_name",
        "source_commit",
        # Logged as MLflow params (Run → Parameters).
        "parameters",
        # Logged as MLflow metrics (Run → Metrics).
        "metrics",
        # mlflow.<flavor>.log_model(...) + optional Model Registry name.
        "log_model",
    }
)

_ALLOWED_CONFIG_KEYS = MLFLOW_CONFIG_RUN_KEYS | MLFLOW_CONFIG_METADATA_KEYS


def project_metadata_fields(merged: dict[str, Any]) -> dict[str, Any]:
    """Subset of merged config applied as run notes/tags (not connection fields)."""
    return {k: merged[k] for k in MLFLOW_CONFIG_METADATA_KEYS if k in merged}


def load_mlflow_config_yaml(path: Path) -> dict[str, Any]:
    """Parse and validate an MLflow settings YAML (no secrets)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"MLflow config file is empty: {path}")
    if not isinstance(raw, dict):
        raise ValueError(f"MLflow config file must be a mapping at the top level: {path}")
    unknown = set(raw) - _ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError(f"Unknown keys in MLflow config file {path}: {', '.join(sorted(unknown))}")
    return raw


def validate_mlflow_config(data: dict[str, Any]) -> None:
    """Validate types for non-secret MLflow config."""
    for key in (
        "tracking_uri",
        "experiment",
        "run_name",
        "run_id",
        "workspace",
        "description",
        "source_script",
        "source_name",
        "source_commit",
    ):
        val = data.get(key)
        if val is not None and not isinstance(val, str):
            raise TypeError(f"{key} must be a string")
    tags = data.get("tags")
    if tags is not None:
        if not isinstance(tags, dict):
            raise TypeError("tags must be a mapping")
        for k, v in tags.items():
            if not isinstance(k, str):
                raise TypeError("tags keys must be strings")
            if v is not None and not isinstance(v, (str | int | float | bool)):
                raise TypeError(f"tags[{k!r}] must be a scalar (string, number, or bool)")
    parameters = data.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, dict):
            raise TypeError("parameters must be a mapping")
        for k, v in parameters.items():
            if not isinstance(k, str):
                raise TypeError("parameters keys must be strings")
            if v is not None and not isinstance(v, (str | int | float | bool)):
                raise TypeError(f"parameters[{k!r}] must be a scalar (string, number, or bool)")
    metrics = data.get("metrics")
    if metrics is not None:
        if not isinstance(metrics, dict):
            raise TypeError("metrics must be a mapping")
        for k, v in metrics.items():
            if not isinstance(k, str):
                raise TypeError("metrics keys must be strings")
            if v is None or isinstance(v, bool) or not isinstance(v, (int | float)):
                raise TypeError(
                    f"metrics[{k!r}] must be a number (int or float), not bool or string"
                )
    log_model = data.get("log_model")
    if log_model is not None:
        if not isinstance(log_model, dict):
            raise TypeError("log_model must be a mapping")
        allowed_lm = frozenset(
            {"flavor", "path", "artifact_path", "loader", "registered_model_name"}
        )
        unknown_lm = set(log_model) - allowed_lm
        if unknown_lm:
            raise TypeError(f"log_model: unknown keys: {', '.join(sorted(unknown_lm))}")
        if log_model.get("flavor") is None or str(log_model.get("flavor")).strip() == "":
            raise TypeError("log_model.flavor is required")
        if log_model.get("path") is None or str(log_model.get("path")).strip() == "":
            raise TypeError("log_model.path is required")
        for key in ("flavor", "path", "artifact_path", "loader", "registered_model_name"):
            val = log_model.get(key)
            if val is not None and not isinstance(val, str):
                raise TypeError(f"log_model.{key} must be a string")
    assert_tracking_uri_has_no_userinfo(data.get("tracking_uri"))
