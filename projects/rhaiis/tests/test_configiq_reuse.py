import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from projects.rhaiis.orchestration import configiq_reuse


class ConfigIQReuseMLflowTests(unittest.TestCase):
    RUN_UUID = "12345678-1234-5678-9234-567812345678"

    def _config(self) -> SimpleNamespace:
        return SimpleNamespace(
            experiment="forge-rhaiis",
            workspace="forge-rhaiis",
            vault_name="mlflow-vault",
            vault_key="mlflow-secret.yaml",
        )

    def test_resolves_unique_tier1_child_and_downloads_its_artifacts(self) -> None:
        client = MagicMock()
        client.get_experiment_by_name.return_value = SimpleNamespace(experiment_id="42")
        client.search_runs.return_value = [
            SimpleNamespace(info=SimpleNamespace(run_id="tier1-child-run"))
        ]
        mlflow_module = MagicMock()
        mlflow_module.tracking.MlflowClient.return_value = client
        mlflow_module.get_tracking_uri.return_value = "https://previous.example"

        with TemporaryDirectory() as directory:
            secret_path = Path(directory) / "mlflow-secret.yaml"
            secret_path.write_text("tracking_uri: https://mlflow.example\n", encoding="utf-8")
            output_dir = Path(directory) / "output"
            previous_workspace = os.environ.get("MLFLOW_WORKSPACE")
            os.environ["MLFLOW_WORKSPACE"] = "original-workspace"
            try:
                with (
                    patch.object(
                        configiq_reuse.vault,
                        "get_vault_content_path",
                        return_value=secret_path,
                    ),
                    patch.dict(sys.modules, {"mlflow": mlflow_module}),
                    patch.object(configiq_reuse, "run_artifacts_import") as artifact_import,
                ):
                    resolved_run_id = configiq_reuse.download_tier1_report_by_uuid.__wrapped__(
                        run_uuid=self.RUN_UUID,
                        output_dir=output_dir,
                        _cfg=self._config(),
                    )
                    restored_workspace = os.environ.get("MLFLOW_WORKSPACE")
            finally:
                if previous_workspace is None:
                    os.environ.pop("MLFLOW_WORKSPACE", None)
                else:
                    os.environ["MLFLOW_WORKSPACE"] = previous_workspace

        self.assertEqual(resolved_run_id, "tier1-child-run")
        client.search_runs.assert_called_once_with(
            experiment_ids=["42"],
            filter_string=(
                f"params.run_uuid = '{self.RUN_UUID}' AND params.configiq_pass = 'tier1'"
            ),
            max_results=2,
        )
        artifact_import.assert_called_once_with(
            mlflow_run_id="tier1-child-run",
            output_dir=output_dir,
            mlflow_tracking_uri="https://mlflow.example",
            mlflow_workspace="forge-rhaiis",
            mlflow_secrets_path=secret_path,
        )
        self.assertEqual(restored_workspace, "original-workspace")
        self.assertEqual(
            mlflow_module.set_tracking_uri.call_args_list[-1].args[0],
            "https://previous.example",
        )

    def test_rejects_missing_or_ambiguous_uuid_matches(self) -> None:
        for runs, error_type, message in (
            ([], FileNotFoundError, "No ConfigIQ Tier 1"),
            (
                [
                    SimpleNamespace(info=SimpleNamespace(run_id="one")),
                    SimpleNamespace(info=SimpleNamespace(run_id="two")),
                ],
                RuntimeError,
                "Multiple ConfigIQ Tier 1",
            ),
        ):
            with self.subTest(error_type=error_type):
                client = MagicMock()
                client.get_experiment_by_name.return_value = SimpleNamespace(experiment_id="42")
                client.search_runs.return_value = runs
                mlflow_module = MagicMock()
                mlflow_module.tracking.MlflowClient.return_value = client
                mlflow_module.get_tracking_uri.return_value = ""
                with TemporaryDirectory() as directory:
                    secret_path = Path(directory) / "mlflow-secret.yaml"
                    secret_path.write_text(
                        "tracking_uri: https://mlflow.example\n",
                        encoding="utf-8",
                    )
                    with (
                        patch.object(
                            configiq_reuse.vault,
                            "get_vault_content_path",
                            return_value=secret_path,
                        ),
                        patch.dict(sys.modules, {"mlflow": mlflow_module}),
                        patch.object(configiq_reuse, "run_artifacts_import"),
                    ):
                        with self.assertRaisesRegex(error_type, message):
                            configiq_reuse.download_tier1_report_by_uuid.__wrapped__(
                                run_uuid=self.RUN_UUID,
                                output_dir=Path(directory) / "output",
                                _cfg=self._config(),
                            )


if __name__ == "__main__":
    unittest.main()
