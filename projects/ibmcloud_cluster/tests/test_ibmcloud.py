"""Offline wrapper regression tests; no IBM resources or credentials."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from projects.core.dsl import script_manager
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.library import env
from projects.ibmcloud_cluster.toolbox.create import main
from projects.ibmcloud_cluster.toolbox.destroy import main as destroy
from projects.ibmcloud_cluster.toolbox.public import (
    CREATE_VAULT_FILES,
    DESTROY_VAULT_FILES,
    ClusterConfig,
    validate_vault_directory,
)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        # Other suites reset the global DSL registry; keep these tests independent
        # of pytest collection/execution order without reloading production modules.
        manager = script_manager.ScriptManager()
        self.enterContext(patch.object(script_manager, "_script_manager", manager))
        for module in (main, destroy):
            for function in vars(module).values():
                if getattr(function, "is_dsl_task", False):
                    info = dict(function._task_info)
                    manager.register_task(info, info["id"].rsplit(":", 1)[0])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.public = self.directory / "public"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "FJOB_NAME": "ibmcloud-test-abcde",
                    "ARTIFACT_DIR": str(self.public),
                    "ARTIFACT_BASE_DIR": str(self.public),
                    "FORGE_BASE_ARTIFACT_DIR": str(self.public),
                },
            )
        )
        self.enterContext(env.TempArtifactDir(self.public))
        self.state = self.directory / "state"
        self.vault_dir = self.directory / "vault_dir"
        self.vault_dir.mkdir()
        for name in CREATE_VAULT_FILES:
            (self.vault_dir / name).write_text("test-credential")
        (self.vault_dir / "pull-secret").write_text(
            json.dumps({"auths": {"example.com": {"auth": "test-credential"}}})
        )
        (self.vault_dir / "ssh-publickey").write_text("ssh-ed25519 AAAADUMMY test\n")
        self.settings = yaml.safe_load(
            (Path(__file__).parents[1] / "orchestration/config.yaml").read_text()
        )["ibmcloud"]
        self.settings.update(
            base_domain="example.com",
            resource_group="test",
        )
        self.settings["release"]["image"] = "quay.io/test/release@sha256:" + "a" * 64
        self.calls = []
        for target, replacement in (
            ("prepare", lambda commit, destination: destination.mkdir()),
            ("defaults", lambda source: {}),
        ):
            mock = patch.object(main.common, target, replacement)
            mock.start()
            self.addCleanup(mock.stop)
        self.enterContext(patch.object(main, "run_step_command", self.run_step_command))
        self.enterContext(patch.object(destroy, "run_step_command", self.run_step_command))

    def run_step_command(self, step, root, runtime, values):
        self.calls.append((step, dict(values)))
        if step == "ibmcloud":
            (root / "shared/install-config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "platform": {"ibmcloud": {}},
                        "controlPlane": {"platform": {"ibmcloud": {}}},
                        "compute": [{"platform": {"ibmcloud": {}}}],
                    }
                )
            )
        if step == "install":
            assets = runtime / "installer"
            assets.mkdir()
            (assets / "metadata.json").write_text(
                json.dumps(
                    {
                        "clusterName": values["CLUSTER_NAME"],
                        "ibmcloud": {"resourceGroupName": values["RESOURCE_GROUP"]},
                    }
                )
            )
            (assets / "private-state").write_text("test-credential")

    def run_action(self, action):
        if action == "destroy":
            return destroy.run(state_dir=str(self.state), vault_dir=str(self.vault_dir))
        settings_file = self.directory / "settings.yaml"
        settings_file.write_text(yaml.safe_dump(self.settings))
        return main.run(
            state_dir=str(self.state),
            vault_dir=str(self.vault_dir),
            settings_file_path=str(settings_file),
            job_name=os.environ["FJOB_NAME"],
        )

    def test_configuration_contract_rejects_invalid_inputs_before_provisioning(self):
        original = copy.deepcopy(self.settings)
        for section, key, value in (
            (None, "region", "invalid region"),
            (None, "zones", ["us-south-1"]),
            (None, "zones", []),
            ("control_plane", "count", 0),
            ("workers", "count", True),
            ("workers", "count", -1),
            ("workers", "type", "invalid profile"),
            ("release", "image", "quay.io/test/release:latest"),
            ("release", "commit", "main"),
            (None, "resource_group", "test\ninvalid"),
            (None, "unexpected", "unknown"),
        ):
            with self.subTest(section=section, key=key):
                self.settings = copy.deepcopy(original)
                target = self.settings if section is None else self.settings[section]
                target[key] = value
                with self.assertRaises(TaskExecutionError):
                    self.run_action("create")
                self.assertFalse(self.state.exists())
                self.assertEqual(self.calls, [])

    def test_configuration_errors_do_not_echo_input_contents(self):
        self.settings["release"]["image"] = "dummy-private-input"
        with self.assertRaises(ValueError) as error:
            ClusterConfig.model_validate(self.settings)
        self.assertNotIn("dummy-private-input", str(error.exception))
        settings_file = self.directory / "broken.yaml"
        settings_file.write_text("value: [dummy-private-input")
        with self.assertRaisesRegex(ValueError, "Invalid cluster settings YAML") as error:
            ClusterConfig.from_yaml(settings_file)
        self.assertNotIn("dummy-private-input", str(error.exception))

    def test_create_vault_contract_matches_bundle_and_rejects_missing_or_empty_files(self):
        definition = yaml.safe_load((env.FORGE_HOME / "vaults/ibmcloud-ipi.yaml").read_text())
        self.assertEqual(set(CREATE_VAULT_FILES), set(definition["content"]))
        missing = self.vault_dir / "ibmcloud-machine-api-api-key"
        missing.unlink()
        with self.assertRaisesRegex(ValueError, missing.name):
            validate_vault_directory(self.vault_dir, for_create=True)
        with self.assertRaises(TaskExecutionError):
            self.run_action("create")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls, [])
        missing.touch()
        with self.assertRaisesRegex(ValueError, "nonempty"):
            validate_vault_directory(self.vault_dir, for_create=True)

    def test_invalid_job_name_is_rejected_before_provisioning(self):
        with patch.dict(os.environ, {"FJOB_NAME": "invalid job/name"}):
            with self.assertRaisesRegex(TaskExecutionError, "job_name"):
                self.run_action("create")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls, [])

    def test_cluster_name_retains_job_identity_and_unique_suffix(self):
        for job_name in ("test", "ibmcloud-create-destroy-abcde", "job." + "x" * 200 + "-abcde"):
            with self.subTest(job_name=job_name):
                first = main.environment(self.settings, self.state, self.vault_dir, job_name)
                second = main.environment(self.settings, self.state, self.vault_dir, job_name)
                self.assertEqual(first["JOB_NAME"], job_name)
                self.assertLessEqual(len(first["CLUSTER_NAME"]), 27)
                self.assertRegex(first["CLUSTER_NAME"], r"^[a-z0-9][a-z0-9-]*-[0-9a-f]{12}$")
                self.assertTrue(first["NAMESPACE"].endswith(job_name[-5:]))
                self.assertEqual(
                    first["CLUSTER_NAME"], f"{first['NAMESPACE']}-{first['UNIQUE_HASH']}"
                )
                self.assertNotEqual(first["CLUSTER_NAME"], second["CLUSTER_NAME"])

    def test_destroy_requires_only_teardown_credentials(self):
        self.run_action("create")
        for name in CREATE_VAULT_FILES:
            if name not in DESTROY_VAULT_FILES:
                (self.vault_dir / name).unlink()
        self.assertEqual(self.run_action("destroy"), {"status": "destroyed"})

    def test_ci_destroy_accepts_reduced_bundle_with_real_vault_validation(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        self.run_action("create")
        for name in CREATE_VAULT_FILES:
            if name not in DESTROY_VAULT_FILES:
                (self.vault_dir / name).unlink()
        with (
            patch.dict(
                os.environ, {"FOURNOS_CI": "true", "IBMCLOUD_IPI_PROFILE_PATH": str(self.vault_dir)}
            ),
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(ci.vault, "_vault_manager", None),
            patch.object(ci.vault, "_strict_validation_enabled", True),
        ):
            self.assertEqual(ci.lifecycle("destroy"), 0)
        self.assertEqual(self.calls[-1][0], "destroy")
        self.assertTrue((self.state / "destroy.succeeded").is_file())

    def test_ci_create_requires_full_bundle_with_real_vault_validation(self):
        from projects.ibmcloud_cluster.orchestration import ci

        (self.vault_dir / "ibmcloud-machine-api-api-key").unlink()
        with (
            patch.dict(
                os.environ, {"FOURNOS_CI": "true", "IBMCLOUD_IPI_PROFILE_PATH": str(self.vault_dir)}
            ),
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(ci.config, "project", SimpleNamespace(get_config=lambda _: self.settings)),
            patch.object(ci.vault, "_vault_manager", None),
            patch.object(ci.vault, "_strict_validation_enabled", True),
        ):
            with self.assertRaisesRegex(ValueError, "ibmcloud-machine-api-api-key"):
                ci.lifecycle("create")
        self.assertFalse((self.directory / "ibmcloud").exists())
        self.assertEqual(self.calls, [])

    def test_ci_destroy_rejects_missing_teardown_files_with_real_vault_validation(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        self.run_action("create")
        for name in DESTROY_VAULT_FILES:
            with self.subTest(filename=name):
                credential = self.vault_dir / name
                original_content = credential.read_text()
                credential.unlink()
                with (
                    patch.dict(
                        os.environ,
                        {"FOURNOS_CI": "true", "IBMCLOUD_IPI_PROFILE_PATH": str(self.vault_dir)},
                    ),
                    patch.object(env, "FORGE_SHARED_DIR", self.directory),
                    patch.object(env, "BASE_ARTIFACT_DIR", self.public),
                    patch.object(ci.vault, "_vault_manager", None),
                    patch.object(ci.vault, "_strict_validation_enabled", True),
                ):
                    with self.assertRaisesRegex(RuntimeError, "mandatory vaults failed validation"):
                        ci.lifecycle("destroy")
                self.assertFalse((self.state / "destroy.succeeded").exists())
                credential.write_text(original_content)
        self.assertNotIn("destroy", [step for step, _ in self.calls])

    def test_ci_missing_teardown_vault_preserves_recovery_details_and_reports_failure(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        self.run_action("create")
        self.vault_dir.rename(self.directory / "unavailable-vault")
        with (
            patch.dict(
                os.environ, {"FOURNOS_CI": "true", "IBMCLOUD_IPI_PROFILE_PATH": str(self.vault_dir)}
            ),
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(env, "BASE_ARTIFACT_DIR", self.public),
            patch.object(ci.vault, "_vault_manager", None),
            patch.object(ci.vault, "_strict_validation_enabled", True),
        ):
            with self.assertRaisesRegex(RuntimeError, "mandatory vaults failed validation"):
                ci.lifecycle("destroy")
        self.assertEqual(
            json.loads((self.public / "ibmcloud-resource-group.json").read_text()),
            {"resourceGroupName": "test"},
        )
        notifications = list(
            (self.public / "000__ci_metadata/notifications").glob("*__ibmcloud-cleanup-failed.txt")
        )
        self.assertEqual(len(notifications), 1)
        self.assertIn("Retain the job, PipelineRun, and private PVC", notifications[0].read_text())
        self.assertTrue((self.state / "install/installer/metadata.json").is_file())
        self.assertTrue((self.state / "install/installer/private-state").is_file())
        self.assertFalse((self.state / "destroy.succeeded").exists())
        self.assertNotIn("destroy", [step for step, _ in self.calls])

    def test_create_destroy_reuses_saved_inputs_and_installer_assets(self):
        created = self.run_action("create")
        self.settings["release"]["image"] = "changed-after-create"
        self.run_action("destroy")
        self.assertEqual(
            [step for step, _ in self.calls],
            ["conf", "ibmcloud", "credentials", "install", "destroy"],
        )
        teardown = self.calls[-1][1]
        self.assertEqual(teardown["SHARED_DIR"], str(self.state / "install/installer"))
        self.assertTrue(teardown["RELEASE_IMAGE_LATEST"].endswith("a" * 64))
        self.assertEqual(created["cluster_name"], teardown["CLUSTER_NAME"])
        self.assertEqual(created["job_name"], "ibmcloud-test-abcde")
        self.assertEqual(teardown["JOB_NAME"], created["job_name"])
        self.assertFalse((self.state / "install").exists())
        self.assertEqual(self.run_action("destroy"), {"status": "destroyed"})

    def test_install_failure_preserves_assets_for_finally(self):
        def fail(step, *args):
            self.run_step_command(step, *args)
            if step == "install":
                raise RuntimeError("test installation failure")

        with patch.object(main, "run_step_command", fail), self.assertRaises(TaskExecutionError):
            self.run_action("create")
        self.assertTrue((self.state / "install/installer/private-state").exists())
        self.assertEqual(self.run_action("destroy")["status"], "destroyed")

    def test_missing_metadata_is_not_successful_cleanup(self):
        self.run_action("create")
        (self.state / "install/installer/metadata.json").unlink()
        with self.assertRaisesRegex(TaskExecutionError, "metadata is missing"):
            self.run_action("destroy")
        self.assertTrue((self.state / "install/installer/private-state").exists())

    def test_destroy_failure_retains_state(self):
        self.run_action("create")
        with patch.object(
            destroy, "run_step_command", side_effect=RuntimeError("test teardown failure")
        ):
            with self.assertRaises(TaskExecutionError):
                self.run_action("destroy")
        self.assertTrue((self.state / "install/installer/private-state").exists())

    def test_finally_before_install_is_a_noop(self):
        self.assertEqual(self.run_action("destroy"), {"status": "not_started"})
        self.assertEqual(self.calls, [])

    def test_dsl_artifacts_do_not_contain_private_file_contents(self):
        self.run_action("create")
        self.run_action("destroy")
        for path in self.public.rglob("*"):
            if path.is_file():
                self.assertNotIn("test-credential", path.read_text())

    def assert_only_public_files_reach_export_backend(self):
        from projects.caliper.engine.file_export import runner

        # Keep live private files present while running the real export collector.
        self.assertTrue((self.state / "install/installer/private-state").exists())
        with patch.object(
            runner.mlflow_backend, "log_artifacts", return_value=("test", {}, [])
        ) as backend:
            result = runner.run_file_export(
                source=self.public,
                backends=["mlflow"],
                dry_run=False,
                mlflow_tracking_uri=None,
                mlflow_experiment=None,
                mlflow_run_id=None,
            )
        self.assertEqual(result[0].status, "success")
        backend.assert_called_once()
        uploaded = backend.call_args.kwargs["paths"]
        self.assertTrue(uploaded)
        for path in uploaded:
            self.assertTrue(path.resolve().is_relative_to(self.public.resolve()))
            self.assertFalse(path.resolve().is_relative_to(self.state.resolve()))
            self.assertNotIn("test-credential", path.read_text())

    def test_export_while_cluster_state_is_present_stays_public(self):
        self.state = self.directory / "internal" / "ibmcloud"
        self.run_action("create")
        self.assert_only_public_files_reach_export_backend()

    def test_export_after_install_failure_stays_public(self):
        def fail(step, *args):
            self.run_step_command(step, *args)
            if step == "install":
                raise RuntimeError("Test installation failure; private state retained")

        with patch.object(main, "run_step_command", fail), self.assertRaises(TaskExecutionError):
            self.run_action("create")
        self.assert_only_public_files_reach_export_backend()

    def test_export_after_destroy_failure_stays_public(self):
        self.run_action("create")
        with patch.object(
            destroy, "run_step_command", side_effect=RuntimeError("Test teardown failure")
        ):
            with self.assertRaises(TaskExecutionError):
                self.run_action("destroy")
        self.assert_only_public_files_reach_export_backend()

    @unittest.skipUnless(
        all(importlib.util.find_spec(name) for name in ("slack_sdk", "jwt", "jsonpath_ng")),
        "Requires Forge export dependencies",
    )
    def test_fournos_default_export_root_remains_inside_public_mount(self):
        from click.testing import CliRunner

        from projects.core.library import config, export

        self.state = self.directory / "internal" / "ibmcloud"
        self.run_action("create")
        source_config = Path(__file__).parents[1] / "orchestration/config.yaml"
        source_contents = source_config.read_bytes()
        # Config.set_config() persists updates to its input path. Exercise that
        # behavior on a temporary copy, never on the repository defaults.
        test_config = self.directory / "export-config.yaml"
        test_config.write_bytes(source_contents)
        project = config.Config(test_config)
        project.config["caliper"] = {"export": {"from": None}}

        def collect(artifact_dir, **kwargs):
            self.assertEqual(artifact_dir.resolve(), self.public.resolve())
            self.assert_only_public_files_reach_export_backend()
            return export.ExportStatus(success=True, final_status="success")

        with (
            env.TempArtifactDir(self.public / "02__export"),
            patch.dict(os.environ, {"FOURNOS_CI": "true"}),
            patch.object(config, "project", project),
            patch.object(
                export, "run_caliper_orchestration_export", side_effect=collect
            ) as collector,
            patch.object(export, "_check_job_shutdown_status", return_value=None),
            patch.object(export, "_update_fjob_export_status"),
            patch.object(export, "_update_final_artifacts"),
        ):
            result = CliRunner().invoke(
                export.caliper_export_entrypoint, ["--disable-notification"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        collector.assert_called_once()
        self.assertEqual(source_config.read_bytes(), source_contents)
        self.assertEqual(
            yaml.safe_load(test_config.read_text())["caliper"]["export"]["from"],
            str(self.public),
        )

    @unittest.skipUnless(
        importlib.util.find_spec("jsonpath_ng"), "Requires Forge config dependencies"
    )
    def test_resource_group_artifact_contains_only_selected_field_without_logging(self):
        from projects.ibmcloud_cluster.orchestration import ci

        metadata_path = self.state / "install/installer/metadata.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "clusterName": "private-cluster-name",
                    "ibmcloud": {"resourceGroupName": "recovery-group", "other": "test-credential"},
                }
            )
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            ci.write_resource_group_artifact(self.state)
        artifact_path = self.public / "ibmcloud-resource-group.json"
        self.assertEqual(
            json.loads(artifact_path.read_text()), {"resourceGroupName": "recovery-group"}
        )
        self.assertEqual(output.getvalue(), "")
        metadata_path.unlink()
        ci.write_resource_group_artifact(self.state)
        self.assertTrue(artifact_path.is_file())

    def test_resource_group_artifact_failure_does_not_skip_cloud_teardown(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        self.run_action("create")
        with (
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(env, "BASE_ARTIFACT_DIR", self.public),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.vault_dir / "ibmcloud-api-key"
            ),
            patch.object(
                ci, "write_resource_group_artifact", side_effect=OSError("Test artifact failure")
            ),
        ):
            with self.assertRaisesRegex(OSError, "Test artifact failure"):
                ci.lifecycle("destroy")
        self.assertEqual(self.calls[-1][0], "destroy")
        self.assertTrue((self.state / "destroy.succeeded").is_file())

    def test_resource_group_artifact_is_saved_after_install_failure(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"

        def fail_install(step, *args):
            self.run_step_command(step, *args)
            if step == "install":
                raise RuntimeError("Test installation failure")

        with (
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(ci.config, "project", SimpleNamespace(get_config=lambda _: self.settings)),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.vault_dir / "ibmcloud-api-key"
            ),
            patch.object(main, "run_step_command", fail_install),
        ):
            with self.assertRaisesRegex(TaskExecutionError, "Test installation failure"):
                ci.lifecycle("create")
        artifact_path = self.public / "ibmcloud-resource-group.json"
        self.assertEqual(json.loads(artifact_path.read_text()), {"resourceGroupName": "test"})
        self.assertTrue((self.state / "install/installer/metadata.json").is_file())

    def test_ci_preserves_install_error_when_recovery_artifact_write_also_fails(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        artifact_path = self.public / "ibmcloud-resource-group.json"
        artifact_path.mkdir(parents=True)
        failure = RuntimeError("dummy-private-installer-output")

        def fail_install(step, *args):
            self.run_step_command(step, *args)
            if step == "install":
                raise failure

        with (
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(ci.config, "project", SimpleNamespace(get_config=lambda _: self.settings)),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.vault_dir / "ibmcloud-api-key"
            ),
            patch.object(main, "run_step_command", fail_install),
            self.assertLogs(ci.__name__, level="WARNING") as logs,
        ):
            with self.assertRaises(TaskExecutionError) as raised:
                ci.lifecycle("create")
        self.assertIs(raised.exception.original_exception, failure)
        self.assertIn("preserving the original installation error", logs.output[0])
        self.assertNotIn("dummy-private-installer-output", "\n".join(logs.output))
        self.assertNotIn("test-credential", "\n".join(logs.output))
        self.assertTrue((self.state / "install/installer/metadata.json").is_file())
        self.assertTrue(artifact_path.is_dir())
        self.assertFalse((self.public / "ibmcloud-status.json").exists())

    def test_ci_reports_artifact_write_failure_after_successful_install(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        artifact_path = self.public / "ibmcloud-resource-group.json"
        artifact_path.mkdir(parents=True)
        with (
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(ci.config, "project", SimpleNamespace(get_config=lambda _: self.settings)),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.vault_dir / "ibmcloud-api-key"
            ),
        ):
            with self.assertRaises(IsADirectoryError):
                ci.lifecycle("create")
        self.assertEqual(self.calls[-1][0], "install")
        self.assertTrue((self.state / "install/installer/metadata.json").is_file())
        self.assertFalse((self.public / "ibmcloud-status.json").exists())

    def test_vault_constant_matches_resolver_configuration(self):
        from projects.ibmcloud_cluster.orchestration import ci

        settings = yaml.safe_load((Path(ci.__file__).parent / "config.yaml").read_text())
        self.assertEqual(settings["vaults"]["all"], [ci.IBM_CLOUD_VAULT])

    @unittest.skipUnless(
        importlib.util.find_spec("jsonpath_ng"), "Requires Forge config dependencies"
    )
    def test_shared_workspace_is_required_only_for_lifecycle(self):
        from projects.ibmcloud_cluster.orchestration import ci

        with patch.object(env, "FORGE_SHARED_DIR", None), patch.object(ci.vault, "init") as init:
            ci.validate_spec({"pipeline": "forge-ibmcloud-create-destroy", "clusterless": True})
            with self.assertRaisesRegex(ValueError, "shared pipeline workspace"):
                ci.lifecycle("create")
        init.assert_not_called()

    @unittest.skipUnless(
        importlib.util.find_spec("jsonpath_ng"), "Requires Forge config dependencies"
    )
    def test_ci_uses_forge_overrides_and_both_dsl_entrypoints(self):
        from projects.core.library import config
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        overrides = self.public / config.VARIABLE_OVERRIDES_FILENAME
        overrides.parent.mkdir(parents=True)
        overrides.write_text(
            yaml.safe_dump(
                {
                    "ibmcloud.base_domain": "example.com",
                    "ibmcloud.resource_group": "test",
                    "ibmcloud.release.image": self.settings["release"]["image"],
                    "ibmcloud.zones": ["au-syd-2"],
                    "ibmcloud.control_plane.count": 3,
                    "ibmcloud.control_plane.type": "bxf-16x64",
                    "ibmcloud.workers.count": 0,
                    "ibmcloud.workers.type": "bxf-8x32",
                }
            )
        )
        with (
            patch.object(config, "project", None),
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.vault_dir / "ibmcloud-api-key"
            ),
        ):
            config.init(Path(ci.__file__).parent, apply_cluster_config=False)
            self.assertEqual(ci.lifecycle("create"), 0)
            generated = yaml.safe_load((self.state / "shared/install-config.yaml").read_text())
            self.assertEqual(
                generated["controlPlane"]["platform"]["ibmcloud"]["zones"], ["au-syd-2"]
            )
            installed = self.calls[-1][1]
            self.assertEqual(installed["CONTROL_PLANE_REPLICAS"], "3")
            self.assertEqual(installed["CONTROL_PLANE_INSTANCE_TYPE"], "bxf-16x64")
            self.assertEqual(installed["WORKERS"], "0")
            self.assertEqual(installed["COMPUTE_NODE_TYPE"], "bxf-8x32")
            self.assertEqual(installed["SIZE_VARIANT"], "compact")
            artifact_path = self.public / "ibmcloud-resource-group.json"
            self.assertEqual(json.loads(artifact_path.read_text()), {"resourceGroupName": "test"})
            artifact_path.unlink()  # Destroy must also save it before removing private state.
            # Teardown must not depend on the current project configuration.
            config.project = None
            self.assertEqual(ci.lifecycle("destroy"), 0)
            self.assertFalse((self.state / "install/installer/metadata.json").exists())
            self.assertEqual(json.loads(artifact_path.read_text()), {"resourceGroupName": "test"})
        self.assertEqual(
            json.loads((self.public / "ibmcloud-status.json").read_text()), {"status": "destroyed"}
        )

    @unittest.skipUnless(
        importlib.util.find_spec("jsonpath_ng"), "Requires Forge config dependencies"
    )
    def test_ci_teardown_failure_registers_safe_notification_and_reraises(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        self.run_action("create")
        failure = RuntimeError("dummy-private-release-output")
        with (
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(env, "BASE_ARTIFACT_DIR", self.public),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.vault_dir / "ibmcloud-api-key"
            ),
            patch.object(ci, "destroy_ibmcloud", side_effect=failure),
        ):
            with self.assertRaises(RuntimeError) as raised:
                ci.lifecycle("destroy")
        self.assertIs(raised.exception, failure)
        self.assertEqual(
            json.loads((self.public / "ibmcloud-resource-group.json").read_text()),
            {"resourceGroupName": "test"},
        )
        notifications = list(
            (self.public / "000__ci_metadata/notifications").glob("*__ibmcloud-cleanup-failed.txt")
        )
        self.assertEqual(len(notifications), 1)
        message = notifications[0].read_text()
        self.assertIn("cleanup failed", message)
        self.assertIn("Retain the job, PipelineRun, and private PVC state", message)
        self.assertNotIn("dummy-private-release-output", message)
        self.assertNotIn("test-credential", message)
        self.assertTrue((self.state / "install/installer/private-state").is_file())

    def test_ci_preserves_teardown_error_when_notification_directory_is_a_file(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        self.run_action("create")
        notifications_path = self.public / "000__ci_metadata/notifications"
        notifications_path.parent.mkdir(parents=True, exist_ok=True)
        notifications_path.write_text("Test obstruction")
        failure = RuntimeError("dummy-private-release-output")
        with (
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(env, "BASE_ARTIFACT_DIR", self.public),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.vault_dir / "ibmcloud-api-key"
            ),
            patch.object(destroy, "run_step_command", side_effect=failure),
            self.assertLogs(ci.__name__, level="WARNING") as logs,
        ):
            with self.assertRaises(TaskExecutionError) as raised:
                ci.lifecycle("destroy")
        self.assertIs(raised.exception.original_exception, failure)
        self.assertIn("preserving the original teardown error", logs.output[0])
        self.assertNotIn("dummy-private-release-output", "\n".join(logs.output))
        self.assertNotIn("test-credential", "\n".join(logs.output))
        self.assertTrue(notifications_path.is_file())
        self.assertTrue((self.state / "install/installer/private-state").is_file())
        self.assertFalse((self.state / "destroy.succeeded").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("jsonpath_ng"), "Requires Forge config dependencies"
    )
    def test_ci_resumes_partial_local_cleanup_without_metadata_or_credentials(self):
        from projects.ibmcloud_cluster.orchestration import ci

        self.state = self.directory / "ibmcloud"
        self.run_action("create")
        remaining = self.state / "install/installer/private-state"
        remove_directory = destroy.shutil.rmtree

        def interrupted_cleanup(path):
            if path == self.state / "install":
                (path / "installer/metadata.json").unlink()
                raise OSError("Test interruption after installer metadata was removed")
            remove_directory(path)

        with patch.object(destroy.shutil, "rmtree", interrupted_cleanup):
            with self.assertRaises(TaskExecutionError):
                self.run_action("destroy")
        self.assertTrue((self.state / "destroy.succeeded").exists())
        self.assertFalse((self.state / "install/installer/metadata.json").exists())
        self.assertTrue(remaining.exists())

        with (
            patch.object(env, "FORGE_SHARED_DIR", self.directory),
            patch.object(
                ci.vault, "init", side_effect=AssertionError("Credentials must not be read")
            ),
            patch.object(
                destroy,
                "run_step_command",
                side_effect=AssertionError("Cloud teardown must not repeat"),
            ),
        ):
            self.assertEqual(ci.lifecycle("destroy"), 0)
        self.assertEqual([name for name, _ in self.calls].count("destroy"), 1)
        self.assertFalse(remaining.exists())
        self.assertEqual({p.name for p in self.state.iterdir() if p.is_dir()}, {"release"})
        self.assertTrue((self.state / "destroy.succeeded").exists())


if __name__ == "__main__":
    unittest.main()
