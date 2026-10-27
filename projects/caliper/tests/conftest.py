"""Shared fixtures for Caliper tests.

Native trace ingestion talks to a real MLflow tracking server, so the tests
must never touch the live server configured in the developer's environment.
The ``mlflow_env_scrubbed`` fixture removes every ``MLFLOW_*`` variable
(credentials, workspace, tracking URI) for the duration of a test, and the
``mlflow_server`` fixture starts a disposable SQLite-backed server on a free
port. No test here contacts an external endpoint.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

# Every MLflow variable that could leak live credentials into a test process.
_MLFLOW_ENV_KEYS = tuple(
    key
    for key in (
        "MLFLOW_TRACKING_URI",
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
        "MLFLOW_TRACKING_TOKEN",
        "MLFLOW_TRACKING_INSECURE_TLS",
        "MLFLOW_TRACKING_SERVER_CERT_PATH",
        "MLFLOW_WORKSPACE",
        "MLFLOW_ENABLE_WORKSPACES",
    )
)


@pytest.fixture()
def mlflow_env_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove MLflow-related environment variables for one test."""
    for key in _MLFLOW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session")
def mlflow_env_session_scrubbed() -> None:
    """Remove MLflow-related environment variables for the whole session.

    Uses os.environ directly (session scope cannot use monkeypatch) and always
    restores the original values, so a live credential never reaches a test
    process while the suite runs.
    """
    saved = {key: os.environ.pop(key, None) for key in _MLFLOW_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(uri: str, timeout_seconds: float = 60.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(uri + "/health", timeout=2)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise RuntimeError(f"MLflow test server did not become ready at {uri}")


@pytest.fixture(scope="session")
def mlflow_server(tmp_path_factory: pytest.TempPathFactory, mlflow_env_session_scrubbed: None):
    """Start a disposable SQLite-backed MLflow server and yield its tracking URI.

    Session-scoped so the ~10s server start is paid once; each test creates its
    own experiment so runs never collide. Tests that need a scrubbed environment
    use the session fixture indirectly through this one.
    """
    tmp_path = tmp_path_factory.mktemp("mlflow-server")
    port = _free_port()
    db_path = tmp_path / "mlflow.db"
    uri = f"http://127.0.0.1:{port}"
    log_file = open(tmp_path / "mlflow-server.log", "w")  # noqa: SIM115 - closed on teardown
    env = {key: value for key, value in os.environ.items() if key not in _MLFLOW_ENV_KEYS}
    process = subprocess.Popen(
        [
            "mlflow",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--backend-store-uri",
            f"sqlite:///{db_path}",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        _wait_for_server(uri)
        yield uri
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()
