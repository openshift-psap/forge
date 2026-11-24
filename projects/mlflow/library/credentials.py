"""Create and reuse Kubernetes credentials for the disposable MLflow test stack."""

from __future__ import annotations

import base64
import json
import re
from secrets import token_hex

from projects.core.dsl.utils.k8s import oc, oc_get_json

POSTGRES_SECRET = "postgresql-credentials"
EXPORTER_SECRET = "postgresql-exporter-credentials"
MINIO_SECRET = "minio-credentials"
MLFLOW_DB_SECRET = "mlflow-db-credentials"
MLFLOW_S3_SECRET = "mlflow-s3-credentials"
ARTIFACT_CONNECTION_SECRET = "mlflow-artifact-connection"
MANAGED_LABEL = "forge.openshift.io/mlflow-test-credentials"

POSTGRES_USER = POSTGRES_DATABASE = "mlflow"
EXPORTER_USER = "pgexporter"
MINIO_USER = "mlflow-minio"


def ensure_storage_credentials(namespace: str) -> None:
    """Generate credentials for a fresh stack and reuse the credentials on a retry."""
    for name, password_key, pvc in (
        (POSTGRES_SECRET, "POSTGRES_PASSWORD", "postgresql-pvc"),
        (EXPORTER_SECRET, "EXPORTER_PASSWORD", "postgresql-pvc"),
        (MINIO_SECRET, "MINIO_ROOT_PASSWORD", "minio-pvc"),
    ):
        existing = oc_get_json(
            "secret", name=name, namespace=namespace, ignore_not_found=True, handled_secretly=True
        )
        if existing is not None:
            _password(existing, name, password_key)
            continue

        if oc_get_json("pvc", name=pvc, namespace=namespace, ignore_not_found=True) is not None:
            raise RuntimeError(f"Cannot generate Secret {name!r} while PVC {pvc!r} exists")

        password = token_hex(32)
        if name == POSTGRES_SECRET:
            data = {
                "POSTGRES_USER": POSTGRES_USER,
                "POSTGRES_DB": POSTGRES_DATABASE,
                password_key: password,
            }
        elif name == EXPORTER_SECRET:
            data = {"EXPORTER_USER": EXPORTER_USER, password_key: password}
        else:
            data = {
                "MINIO_ROOT_USER": MINIO_USER,
                password_key: password,
                "MC_HOST_local": f"http://{MINIO_USER}:{password}@minio.{namespace}.svc:9000",
            }
        _write_secret(namespace, name, data, verb="create")


def apply_mlflow_credentials(namespace: str, operator_namespace: str) -> None:
    """Create the Secret references used by MLflow and its artifact connection."""
    postgres_password = _read_password(namespace, POSTGRES_SECRET, "POSTGRES_PASSWORD")
    minio_password = _read_password(namespace, MINIO_SECRET, "MINIO_ROOT_PASSWORD")
    s3_endpoint = f"http://minio.{namespace}.svc:9000"
    s3_keys = {"AWS_ACCESS_KEY_ID": MINIO_USER, "AWS_SECRET_ACCESS_KEY": minio_password}
    _write_secret(
        operator_namespace,
        MLFLOW_DB_SECRET,
        {
            "backend-store-uri": (
                f"postgresql://{POSTGRES_USER}:{postgres_password}"
                f"@postgresql.{namespace}.svc:5432/{POSTGRES_DATABASE}?sslmode=disable"
            ),
        },
    )
    _write_secret(
        operator_namespace, MLFLOW_S3_SECRET, {**s3_keys, "AWS_S3_ENDPOINT_URL": s3_endpoint}
    )
    _write_secret(
        namespace,
        ARTIFACT_CONNECTION_SECRET,
        {**s3_keys, "AWS_S3_BUCKET": "mlflow-artifacts", "AWS_S3_ENDPOINT": s3_endpoint},
        annotations={"opendatahub.io/connection-type-protocol": "s3"},
    )


def ensure_exporter_role(namespace: str) -> None:
    """Create the monitoring login and set its password through protected stdin."""
    password = _read_password(namespace, EXPORTER_SECRET, "EXPORTER_PASSWORD")
    # The generated hex password is safe to interpolate as a SQL string literal.
    sql = f"""
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SET log_min_error_statement = 'panic';
SET log_duration = off;
SET pg_stat_statements.track = 'none';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{EXPORTER_USER}') THEN
        CREATE ROLE {EXPORTER_USER} LOGIN;
    END IF;
END $$;
ALTER ROLE {EXPORTER_USER} PASSWORD '{password}';
GRANT pg_monitor TO {EXPORTER_USER};
GRANT CONNECT ON DATABASE {POSTGRES_DATABASE} TO {EXPORTER_USER};
"""
    oc(
        "exec",
        "-i",
        "-n",
        namespace,
        "deployment/postgresql",
        "--",
        "psql",
        "-X",
        "-U",
        "postgres",
        "-d",
        POSTGRES_DATABASE,
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        "-",
        input_text=sql,
        handled_secretly=True,
    )


def _read_password(namespace: str, name: str, key: str) -> str:
    secret = oc_get_json("secret", name=name, namespace=namespace, handled_secretly=True)
    return _password(secret, name, key)


def _password(secret: dict, name: str, key: str) -> str:
    if secret.get("metadata", {}).get("labels", {}).get(MANAGED_LABEL) != "true":
        raise RuntimeError(f"Secret {name!r} is not managed by the MLflow test stack")
    try:
        password = base64.b64decode(secret["data"][key], validate=True).decode("ascii")
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"Secret {name!r} has an invalid {key} entry") from None
    if re.fullmatch(r"[0-9a-f]{64}", password) is None:
        raise RuntimeError(f"Secret {name!r} has an invalid {key} entry")
    return password


def _write_secret(
    namespace: str,
    name: str,
    data: dict[str, str],
    *,
    verb: str = "apply",
    annotations: dict[str, str] | None = None,
) -> None:
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {MANAGED_LABEL: "true"},
            "annotations": annotations or {},
        },
        "type": "Opaque",
        "stringData": data,
    }
    oc(verb, "-f", "-", input_text=json.dumps(manifest), handled_secretly=True)
