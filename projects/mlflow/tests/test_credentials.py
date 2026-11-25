import base64
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlparse

from projects.mlflow.library import credentials
from projects.mlflow.toolbox.apply_infrastructure.main import _apply_docs, _render_docs
from projects.mlflow.toolbox.cleanup_test_resources.main import delete_test_credentials


class TestStorageCredentials(TestCase):
    def setUp(self):
        self.secrets = {}
        self.pvcs = set()
        self.enterContext(patch.object(credentials, "oc_get_json", side_effect=self._get_json))
        self.oc = self.enterContext(patch.object(credentials, "oc", side_effect=self._oc))

    def _get_json(self, kind, *, name, namespace, **kwargs):
        if kind == "pvc":
            return {} if name in self.pvcs else None
        self.assertTrue(kwargs["handled_secretly"])
        return self.secrets.get((namespace, name))

    def _oc(self, verb, *args, input_text, handled_secretly):
        self.assertTrue(handled_secretly)
        if verb != "exec":
            self.assertEqual(args, ("-f", "-"))
            manifest = json.loads(input_text)
            metadata = manifest["metadata"]
            manifest["data"] = {
                key: base64.b64encode(value.encode()).decode()
                for key, value in manifest.pop("stringData").items()
            }
            self.secrets[metadata["namespace"], metadata["name"]] = manifest

    def _secret_data(self, namespace, name):
        return {
            key: base64.b64decode(value).decode()
            for key, value in self.secrets[namespace, name]["data"].items()
        }

    def test_fresh_credentials_are_independent_and_retry_reuses_them(self):
        credentials.ensure_storage_credentials("benchmark")
        passwords = [
            self._secret_data("benchmark", name)[key]
            for name, key in (
                (credentials.POSTGRES_SECRET, "POSTGRES_PASSWORD"),
                (credentials.EXPORTER_SECRET, "EXPORTER_PASSWORD"),
                (credentials.MINIO_SECRET, "MINIO_ROOT_PASSWORD"),
            )
        ]
        self.assertEqual(len(set(passwords)), 3)
        for password in passwords:
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", password), "Invalid generated password")

        self.pvcs.update(("postgresql-pvc", "minio-pvc"))
        credentials.ensure_storage_credentials("benchmark")
        self.assertEqual(self.oc.call_count, 3)

        self.secrets.clear()
        self.pvcs.clear()
        credentials.ensure_storage_credentials("benchmark")
        new_password = self._secret_data("benchmark", credentials.POSTGRES_SECRET)[
            "POSTGRES_PASSWORD"
        ]
        self.assertTrue(new_password not in passwords, "A fresh stack reused a password")

    def test_missing_secret_with_a_live_pvc_blocks_regeneration(self):
        self.pvcs.add("postgresql-pvc")
        with self.assertRaisesRegex(RuntimeError, "while PVC"):
            credentials.ensure_storage_credentials("benchmark")
        self.oc.assert_not_called()

    def test_operator_and_artifact_secrets_use_the_generated_passwords(self):
        credentials.ensure_storage_credentials("benchmark")
        credentials.apply_mlflow_credentials("benchmark", "operator")
        postgres = self._secret_data("benchmark", credentials.POSTGRES_SECRET)
        minio = self._secret_data("benchmark", credentials.MINIO_SECRET)
        db_secret = self._secret_data("operator", credentials.MLFLOW_DB_SECRET)
        self.assertTrue(
            urlparse(db_secret["backend-store-uri"]).password == postgres["POSTGRES_PASSWORD"],
            "The MLflow database password does not match its PostgreSQL Secret",
        )
        s3_secret = self._secret_data("operator", credentials.MLFLOW_S3_SECRET)
        artifact = self._secret_data("benchmark", credentials.ARTIFACT_CONNECTION_SECRET)
        for secret in (s3_secret, artifact):
            self.assertEqual(secret["AWS_ACCESS_KEY_ID"], minio["MINIO_ROOT_USER"])
            self.assertTrue(
                secret["AWS_SECRET_ACCESS_KEY"] == minio["MINIO_ROOT_PASSWORD"],
                "The S3 password does not match its MinIO Secret",
            )
        self.assertTrue(
            urlparse(minio["MC_HOST_local"]).password == minio["MINIO_ROOT_PASSWORD"],
            "The bucket Job password does not match its MinIO Secret",
        )

    def test_exporter_password_is_only_sent_through_protected_stdin(self):
        credentials.ensure_storage_credentials("benchmark")
        credentials.ensure_exporter_role("benchmark")
        password = self._secret_data("benchmark", credentials.EXPORTER_SECRET)["EXPORTER_PASSWORD"]
        command = self.oc.call_args
        self.assertTrue(command.kwargs["handled_secretly"])
        self.assertTrue(password in command.kwargs["input_text"], "Missing password in stdin")
        self.assertTrue(all(password not in argument for argument in command.args))


class TestCredentialManifests(TestCase):
    def test_minio_and_bucket_job_reference_the_secret(self):
        args = SimpleNamespace(namespace="benchmark", operator_namespace="operator")
        deployment = next(
            doc for doc in _render_docs(args, "01-minio.yaml") if doc["kind"] == "Deployment"
        )
        (job,) = _render_docs(args, "07-minio-create-bucket.yaml")
        for manifest in (deployment, job):
            (container,) = manifest["spec"]["template"]["spec"]["containers"]
            for variable in container["env"]:
                reference = variable["valueFrom"]["secretKeyRef"]
                self.assertEqual(reference["name"], credentials.MINIO_SECRET)
                self.assertEqual(reference["key"], variable["name"])

    def test_artifact_writer_rejects_secrets(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "resource.yaml"
            with self.assertRaisesRegex(ValueError, "Secret manifests"):
                _apply_docs(path, [{"kind": "Secret"}])
            self.assertFalse(path.exists())

    def test_cleanup_keeps_credentials_when_storage_is_still_present(self):
        args = SimpleNamespace(namespace="benchmark", operator_namespace="operator")
        with (
            patch(
                "projects.mlflow.toolbox.cleanup_test_resources.main.oc",
                return_value=SimpleNamespace(stdout="persistentvolumeclaim/postgresql-pvc"),
            ) as oc,
            self.assertRaisesRegex(RuntimeError, "while resources remain"),
        ):
            delete_test_credentials.original_func(args, None)
        self.assertTrue(all(call.args[0] == "get" for call in oc.call_args_list))

    def test_cleanup_deletes_credentials_from_both_namespaces(self):
        args = SimpleNamespace(namespace="benchmark", operator_namespace="operator")
        with patch(
            "projects.mlflow.toolbox.cleanup_test_resources.main.oc",
            return_value=SimpleNamespace(stdout=""),
        ) as oc:
            delete_test_credentials.original_func(args, None)
        deletes = [call.args for call in oc.call_args_list if call.args[0] == "delete"]
        self.assertEqual(
            {argument for call in deletes for argument in call[2 : call.index("-n")]},
            {
                credentials.POSTGRES_SECRET,
                credentials.EXPORTER_SECRET,
                credentials.MINIO_SECRET,
                credentials.MLFLOW_DB_SECRET,
                credentials.MLFLOW_S3_SECRET,
                credentials.ARTIFACT_CONNECTION_SECRET,
            },
        )
        self.assertEqual(
            [call[call.index("-n") + 1] for call in deletes], ["operator", "benchmark"]
        )
