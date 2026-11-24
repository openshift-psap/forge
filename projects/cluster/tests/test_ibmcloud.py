"""Offline wrapper regression tests; no IBM resources or credentials."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from projects.cluster.toolbox.ibmcloud import destroy, main
from projects.core.dsl import script_manager
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.library import env


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
                    "ARTIFACT_DIR": str(self.public),
                    "ARTIFACT_BASE_DIR": str(self.public),
                    "FORGE_BASE_ARTIFACT_DIR": str(self.public),
                },
            )
        )
        self.enterContext(env.TempArtifactDir(self.public))
        self.state = self.directory / "state"
        self.profile = self.directory / "profile"
        self.profile.mkdir()
        for name in ("ibmcloud-api-key", "ssh-privatekey"):
            (self.profile / name).write_text("test-credential")
        self.settings = yaml.safe_load(
            (Path(__file__).parents[1] / "orchestration/config.yaml").read_text()
        )["ibmcloud"]
        self.settings.update(
            base_domain="example.com",
            resource_group="test",
            release_image="quay.io/test/release@sha256:" + "a" * 64,
        )
        self.calls = []
        for target, replacement in (
            ("prepare", lambda commit, destination: destination.mkdir()),
            ("defaults", lambda source: {}),
        ):
            mock = patch.object(main.upstream, target, replacement)
            mock.start()
            self.addCleanup(mock.stop)
        self.enterContext(patch.object(main, "execute", self.execute))
        self.enterContext(patch.object(destroy, "execute", self.execute))

    def execute(self, step, root, runtime, values):
        self.calls.append((step, dict(values)))
        if step == "ibmcloud":
            (root / "shared/install-config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "controlPlane": {"platform": {"ibmcloud": {}}},
                        "compute": [{"platform": {"ibmcloud": {}}}],
                    }
                )
            )
        if step == "install":
            assets = runtime / "installer"
            assets.mkdir()
            (assets / "metadata.json").write_text(
                json.dumps({"clusterName": f"forge-{values['UNIQUE_HASH']}"})
            )
            (assets / "private-state").write_text("test-credential")

    def run_action(self, action):
        if action == "destroy":
            return destroy.run(state_dir=str(self.state), profile_dir=str(self.profile))
        return main.run(
            state_dir=str(self.state),
            profile_dir=str(self.profile),
            settings=self.settings,
        )

    def test_create_destroy_reuses_saved_inputs_and_installer_assets(self):
        created = self.run_action("create")
        self.settings["release_image"] = "changed-after-create"
        self.run_action("destroy")
        self.assertEqual(
            [step for step, _ in self.calls],
            ["conf", "ibmcloud", "credentials", "install", "destroy"],
        )
        teardown = self.calls[-1][1]
        self.assertEqual(teardown["SHARED_DIR"], str(self.state / "install/installer"))
        self.assertTrue(teardown["RELEASE_IMAGE_LATEST"].endswith("a" * 64))
        self.assertEqual(created["cluster_name"], f"forge-{teardown['UNIQUE_HASH']}")
        self.assertFalse((self.state / "install").exists())
        self.assertEqual(self.run_action("destroy"), {"status": "destroyed"})

    def test_install_failure_preserves_assets_for_finally(self):
        def fail(step, *args):
            self.execute(step, *args)
            if step == "install":
                raise RuntimeError("test installation failure")

        with patch.object(main, "execute", fail), self.assertRaises(TaskExecutionError):
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
        with patch.object(destroy, "execute", side_effect=RuntimeError("test teardown failure")):
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
            self.execute(step, *args)
            if step == "install":
                raise RuntimeError("Test installation failure; private state retained")

        with patch.object(main, "execute", fail), self.assertRaises(TaskExecutionError):
            self.run_action("create")
        self.assert_only_public_files_reach_export_backend()

    def test_export_after_destroy_failure_stays_public(self):
        self.run_action("create")
        with patch.object(destroy, "execute", side_effect=RuntimeError("Test teardown failure")):
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
        project = config.Config(Path(__file__).parents[1] / "orchestration/config.yaml")
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

    @unittest.skipUnless(
        importlib.util.find_spec("jsonpath_ng"), "Requires Forge config dependencies"
    )
    def test_ci_uses_forge_overrides_and_both_dsl_entrypoints(self):
        from projects.cluster.orchestration import ci
        from projects.core.library import config

        self.state = self.directory / "ibmcloud"
        overrides = self.public / config.VARIABLE_OVERRIDES_FILENAME
        overrides.parent.mkdir(parents=True)
        overrides.write_text(
            yaml.safe_dump(
                {
                    "ibmcloud.base_domain": "example.com",
                    "ibmcloud.resource_group": "test",
                    "ibmcloud.release_image": self.settings["release_image"],
                    "ibmcloud.zones": ["au-syd-2"],
                }
            )
        )
        with (
            patch.object(config, "project", None),
            patch.dict(
                os.environ,
                {
                    "FORGE_INTERNAL_DIR": str(self.directory),
                },
            ),
            patch.object(ci.vault, "init"),
            patch.object(
                ci.vault, "get_vault_content_path", return_value=self.profile / "ibmcloud-api-key"
            ),
        ):
            config.init(Path(ci.__file__).parent, apply_cluster_config=False)
            self.assertEqual(ci.lifecycle("create"), 0)
            generated = yaml.safe_load((self.state / "shared/install-config.yaml").read_text())
            self.assertEqual(
                generated["controlPlane"]["platform"]["ibmcloud"]["zones"], ["au-syd-2"]
            )
            # Teardown must not depend on the current project configuration.
            config.project = None
            self.assertEqual(ci.lifecycle("destroy"), 0)
        self.assertEqual(
            json.loads((self.public / "ibmcloud-status.json").read_text()), {"status": "destroyed"}
        )


if __name__ == "__main__":
    unittest.main()
