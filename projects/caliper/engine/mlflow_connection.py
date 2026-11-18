"""Read, validate, and apply Caliper MLflow connections."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml


def assert_tracking_uri_has_no_userinfo(uri: str | None, *, field: str = "tracking_uri") -> None:
    """
    Reject URIs with embedded userinfo (``scheme://user:pass@host``).

    MLflow credentials must use ``token`` / ``username`` / ``password`` fields or env vars,
    not the authority component of the URI. Error messages must not include the URI.
    """
    if not uri or not isinstance(uri, str):
        return
    u = uri.strip()
    if not u:
        return
    try:
        parsed = urlparse(u)
    except Exception as e:
        raise ValueError(f"{field} is not a valid URI") from e
    if not parsed.netloc:
        return
    if "@" in parsed.netloc:
        raise ValueError(
            f"{field} must not include embedded credentials; "
            "use token or username/password fields instead of userinfo in the URL"
        )


# Keys we set in os.environ while the context is active (restored on exit).
_ENV_KEYS = (
    "MLFLOW_TRACKING_URI",
    "MLFLOW_TRACKING_TOKEN",
    "MLFLOW_TRACKING_USERNAME",
    "MLFLOW_TRACKING_PASSWORD",
    "MLFLOW_TRACKING_INSECURE_TLS",
    "MLFLOW_TRACKING_SERVER_CERT_PATH",
)

# Credentials and TLS only — use mlflow_config.py for experiment, run_name, run_id, etc.
_ALLOWED_YAML_KEYS = frozenset(
    {
        "tracking_uri",
        "token",
        "username",
        "password",
        "insecure_tls",
        "server_cert_path",
    }
)


def _read_mlflow_secrets_yaml(path: Path) -> dict[str, Any]:
    """Read the credential document and verify its top-level shape."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        raise ValueError(f"Invalid YAML in MLflow secrets file: {path}") from None
    if raw is None:
        raise ValueError(f"MLflow secrets file is empty: {path}")
    if not isinstance(raw, dict):
        raise ValueError(f"MLflow secrets file must be a mapping at the top level: {path}")
    return raw


def load_mlflow_connection_file(
    path: Path, *, mlflow_instance: str | None = None
) -> dict[str, Any]:
    """Prepare the selected MLflow credentials for callers as a flat mapping."""
    raw = _read_mlflow_secrets_yaml(path)
    if mlflow_instance is not None:
        if mlflow_instance not in raw:
            raise ValueError(f"MLflow profile {mlflow_instance!r} is missing from {path}")
        raw = raw[mlflow_instance]
        if not isinstance(raw, dict):
            raise ValueError(f"MLflow profile {mlflow_instance!r} must be a mapping: {path}")
        uri = raw.get("tracking_uri")
        if not isinstance(uri, str) or not uri.strip():
            raise ValueError(f"MLflow profile {mlflow_instance!r} needs a tracking_uri: {path}")
    unknown = set(raw) - _ALLOWED_YAML_KEYS
    if unknown:
        raise ValueError(
            f"Unknown keys in MLflow secrets file {path}: {', '.join(sorted(unknown))}"
        )
    return raw


def validate_mlflow_secrets(data: dict[str, Any]) -> None:
    """Ensure auth and URI constraints are satisfied."""
    uri = data.get("tracking_uri")
    if uri is not None and not isinstance(uri, str):
        raise TypeError("tracking_uri must be a string")
    token = data.get("token")
    username = data.get("username")
    password = data.get("password")
    for name, val in ("token", token), ("username", username), ("password", password):
        if val is not None and not isinstance(val, str):
            raise TypeError(f"{name} must be a string")
    has_token = bool(token)
    has_basic = bool(username or password)
    if has_basic and has_token:
        raise ValueError(
            "Use either token or username/password in the MLflow secrets file, not both"
        )
    if has_basic and not (username and password):
        raise ValueError("username and password must both be set for basic authentication")
    if data.get("insecure_tls") is not None and not isinstance(data["insecure_tls"], bool):
        raise TypeError("insecure_tls must be a boolean")
    scp = data.get("server_cert_path")
    if scp is not None and not isinstance(scp, str):
        raise TypeError("server_cert_path must be a string")
    assert_tracking_uri_has_no_userinfo(uri)


def project_secrets_fields(merged: dict[str, Any]) -> dict[str, Any]:
    """Subset of ``merged`` that belongs in the secrets file / credential env (for ``connection_to_env``)."""
    return {k: merged[k] for k in _ALLOWED_YAML_KEYS if k in merged}


def connection_to_env(conn: dict[str, Any]) -> dict[str, str]:
    """Build os.environ updates from merged connection (only keys that are set)."""
    out: dict[str, str] = {}
    if uri := conn.get("tracking_uri"):
        assert_tracking_uri_has_no_userinfo(uri)
        out["MLFLOW_TRACKING_URI"] = uri
    if conn.get("token"):
        out["MLFLOW_TRACKING_TOKEN"] = conn["token"]
    elif conn.get("username") and conn.get("password"):
        out["MLFLOW_TRACKING_USERNAME"] = conn["username"]
        out["MLFLOW_TRACKING_PASSWORD"] = conn["password"]
    if conn.get("insecure_tls"):
        out["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    if path := conn.get("server_cert_path"):
        out["MLFLOW_TRACKING_SERVER_CERT_PATH"] = path
    return out


def resolve_mlflow_tracking_uri(
    connection: dict[str, Any], requested_uri: str | None = None
) -> str | None:
    """Use the connection's endpoint and reject any conflicting requested endpoint."""
    uri = connection.get("tracking_uri")
    if uri and requested_uri and uri.rstrip("/") != requested_uri.rstrip("/"):
        raise ValueError("Requested MLflow tracking URI does not match the selected connection")
    selected_uri = uri or requested_uri
    assert_tracking_uri_has_no_userinfo(selected_uri)
    return selected_uri


@contextmanager
def mlflow_connection_env(conn: dict[str, Any]) -> Iterator[None]:
    """
    Apply MLflow-related environment variables for this process, then restore previous values.

    Tracked keys are cleared first so a token from the parent shell cannot leak when the file
    uses basic auth (or vice versa).
    """
    env_updates = connection_to_env(conn)
    previous: dict[str, str | None] = {k: os.environ.get(k) for k in _ENV_KEYS}
    try:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in env_updates.items():
            os.environ[k] = v
        yield
    finally:
        for k in _ENV_KEYS:
            if previous[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = previous[k]  # type: ignore[assignment]


@contextmanager
def mlflow_tracking_context(
    connection: dict[str, Any], *, workspace: str | None = None
) -> Iterator[None]:
    """Apply a connection for an MLflow operation and restore the caller's state."""
    import mlflow

    previous_uri = mlflow.get_tracking_uri()
    previous_workspace = os.environ.get("MLFLOW_WORKSPACE")
    with mlflow_connection_env(connection):
        try:
            if uri := connection.get("tracking_uri"):
                mlflow.set_tracking_uri(uri)
            if workspace is not None:
                os.environ["MLFLOW_WORKSPACE"] = workspace
            yield
        finally:
            mlflow.set_tracking_uri(previous_uri)
            if previous_workspace is None:
                os.environ.pop("MLFLOW_WORKSPACE", None)
            else:
                os.environ["MLFLOW_WORKSPACE"] = previous_workspace


def parse_mlflow_url(url: str) -> dict[str, str | None]:
    """
    Parse MLflow web UI URL and extract components.

    Supports URLs like:
    https://mlflow.apps.example.com/#/experiments/231/runs/3147e102.../artifacts/path?workspace=forge

    Returns dict with: endpoint, experiment_id, run_id, artifact_path, workspace
    """
    try:
        parsed = urlparse(url)

        # Extract base endpoint (scheme + netloc + path)
        endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        # Parse fragment (the part after #) - this may contain query params too
        fragment = parsed.fragment or ""
        workspace = None
        artifact_path = None
        experiment_id = None
        run_id = None

        # Split fragment into path and query parts
        if "?" in fragment:
            fragment_path, fragment_query = fragment.split("?", 1)
            # Parse fragment query parameters (for workspace)
            fragment_params = parse_qs(fragment_query)
            workspace = fragment_params.get("workspace", [None])[0]
        else:
            fragment_path = fragment

        # Also check main URL query parameters (fallback)
        if not workspace and parsed.query:
            query_params = parse_qs(parsed.query)
            workspace = query_params.get("workspace", [None])[0]

        # Parse the fragment path: /experiments/231/runs/3147e102.../artifacts/path
        if fragment_path.startswith("/"):
            fragment_path = fragment_path[1:]  # Remove leading slash

        parts = fragment_path.split("/")

        # Expected patterns:
        # 1. experiments/231/runs/RUN_ID/artifacts/PATH (standard)
        # 2. experiments/231/runs/RUN_ID/PATH (direct artifact path)
        if len(parts) >= 4 and parts[0] == "experiments" and parts[2] == "runs":
            experiment_id = parts[1]
            run_id = parts[3]

            # Check for artifacts path
            if len(parts) > 4:
                if parts[4] == "artifacts":
                    # Standard format: /experiments/231/runs/RUN_ID/artifacts/PATH
                    if len(parts) > 5:
                        artifact_path = "/".join(parts[5:])
                else:
                    # Alternative format: /experiments/231/runs/RUN_ID/PATH
                    artifact_path = "/".join(parts[4:])

        return {
            "tracking_uri": endpoint,
            "experiment": experiment_id,
            "run_id": run_id,
            "artifact_path": artifact_path,
            "workspace": workspace,
        }

    except Exception as e:
        raise ValueError(f"Failed to parse MLflow URL: {e}") from e
