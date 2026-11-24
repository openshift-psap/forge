"""Exercise pinned release scripts locally, with dummy keys and no cloud calls.

The adjacent release checkout supplies committed source, never working-tree edits.
Tests skip when it is absent. The real installer binary is never run.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from projects.cluster.toolbox.ibmcloud import main, upstream
from projects.core.library import env


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.repo = env.FORGE_HOME.parent / "release"
        if not (self.repo / ".git").exists():
            self.skipTest("Requires adjacent openshift/release checkout")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = yaml.safe_load(
            (env.FORGE_HOME / "projects/cluster/orchestration/config.yaml").read_text()
        )["ibmcloud"]
        self.settings.update(
            base_domain="example.com",
            resource_group="test",
            release_image="quay.io/test/release@sha256:" + "a" * 64,
        )
        self.profile = self.root / "profile"
        self.profile.mkdir()
        definition = yaml.safe_load((env.FORGE_HOME / "vaults/ibmcloud-ipi.yaml").read_text())
        for name in definition["content"]:
            (self.profile / name).write_text("dummy-test-credential")
        (self.profile / "pull-secret").write_text(
            json.dumps({"auths": {"example.com": {"auth": "dummy"}}})
        )
        (self.profile / "ssh-publickey").write_text("ssh-ed25519 AAAADUMMY test\n")
        for name in ("shared", "logs", "install"):
            (self.root / name).mkdir()
        self.urls = []

        def committed_source(url, timeout):
            self.urls.append(url)
            prefix = f"https://raw.githubusercontent.com/openshift/release/{self.settings['release_commit']}/"
            self.assertTrue(url.startswith(prefix))
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo),
                    "show",
                    f"{self.settings['release_commit']}:{url.removeprefix(prefix)}",
                ],
                check=True,
                capture_output=True,
            )
            return io.BytesIO(result.stdout)

        # Only source retrieval is replaced: configuration and teardown run as Bash subprocesses.
        with patch.object(upstream.urllib.request, "urlopen", committed_source):
            upstream.prepare(self.settings["release_commit"], self.root / "release")
        self.values = main.environment(self.settings, self.root, self.profile)

    def test_pinned_sources_adapt_before_any_cloud_operation(self):
        self.assertEqual(len(self.urls), 10)
        install = (self.root / "release/install-commands.sh").read_text()
        self.assertIn('dir="${FORGE_RUNTIME_DIR}/installer"', install)
        self.assertNotIn("dir=/tmp/installer", install)
        self.assertIn("${CUSTOM_OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE%@*}", install)
        teardown = (self.root / "release/destroy-commands.sh").read_text()
        self.assertNotIn("--dir /tmp/installer", teardown)
        self.assertEqual(self.values["FIPS_ENABLED"], "false")

    def test_real_configuration_scripts_with_dummy_credentials(self):
        for step in ("conf", "ibmcloud", "credentials"):
            main.execute(step, self.root, self.root / "install", self.values)
        config = yaml.safe_load((self.root / "shared/install-config.yaml").read_text())
        self.assertEqual(config["platform"]["ibmcloud"]["region"], "au-syd")
        self.assertEqual(config["controlPlane"]["platform"]["ibmcloud"]["type"], "bxf-8x32")
        self.assertEqual(config["compute"][0]["platform"]["ibmcloud"]["type"], "bxf-16x64")
        self.assertEqual(config["controlPlane"]["replicas"], 3)
        self.assertEqual(config["compute"][0]["replicas"], 3)
        manifests = list((self.root / "shared").glob("manifest_*.yaml"))
        self.assertEqual(len(manifests), 5)
        for manifest in manifests:
            self.assertEqual(yaml.safe_load(manifest.read_text())["kind"], "Secret")

    def configure_cluster(self):
        args = SimpleNamespace(settings=self.settings)
        context = SimpleNamespace(
            root=self.root,
            profile=self.profile,
            runtime=self.root / "install",
            values=main.environment(self.settings, self.root, self.profile),
        )
        main.prepare_config_profile.original_func(args, context)
        main.configure_cluster.original_func(args, context)
        return yaml.safe_load((self.root / "shared/install-config.yaml").read_text())

    def test_multiline_pull_secret_is_normalized_without_modifying_vault(self):
        secret = {"auths": {"example.com": {"auth": "dummy-test-credential"}}}
        original = json.dumps(secret, indent=2)
        source = self.profile / "pull-secret"
        source.write_text(original)
        source.chmod(0o444)
        config = self.configure_cluster()
        self.assertEqual(json.loads(config["pullSecret"]), secret)
        self.assertEqual(source.read_text(), original)
        self.assertEqual(source.stat().st_mode & 0o777, 0o444)
        normalized = self.root / "config-profile/pull-secret"
        self.assertEqual(json.loads(normalized.read_text()), secret)
        self.assertNotIn("\n", normalized.read_text())
        self.assertEqual(normalized.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config["platform"]["ibmcloud"]["resourceGroupName"], "test")

    def test_boolean_like_resource_group_remains_a_string(self):
        self.settings["resource_group"] = "on"
        config = self.configure_cluster()
        resource_group = config["platform"]["ibmcloud"]["resourceGroupName"]
        self.assertIsInstance(resource_group, str)
        self.assertEqual(resource_group, "on")

    def test_real_teardown_script_with_fake_oc_and_installer(self):
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "metadata.json").write_text('{"clusterName":"test"}')
        (assets / "private-state").write_text("dummy-test-credential")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        # Forge runs on Linux; use GNU cp locally when macOS provides BSD cp.
        if os.uname().sysname == "Darwin":
            gnu_cp = shutil.which("gcp")
            if not gnu_cp:
                self.skipTest("Requires GNU cp, as installed in the Forge image")
            (fake_bin / "cp").symlink_to(gnu_cp)
        installer = fake_bin / "fake-installer"
        installer.write_text("""#!/bin/bash
set -eu
if [[ "$*" == version ]]; then exit 0; fi
[[ "$*" == *"destroy cluster"* ]] || exit 97
test -s "$FORGE_RUNTIME_DIR/installer/metadata.json"
test -s "$FORGE_RUNTIME_DIR/installer/private-state"
echo "mock deletion" > "$FORGE_RUNTIME_DIR/installer/.openshift_install.log"
touch "$FAKE_DESTROY_MARKER"
""")
        oc = fake_bin / "oc"
        oc.write_text("""#!/bin/bash
set -eu
[[ "$*" == *"--command=openshift-install"* ]] || exit 98
cp "$FAKE_INSTALLER" "$FORGE_RUNTIME_DIR/openshift-install"
chmod +x "$FORGE_RUNTIME_DIR/openshift-install"
""")
        oc.chmod(0o700)
        installer.chmod(0o700)
        runtime = self.root / "destroy"
        runtime.mkdir()
        marker = self.root / "mock-deletion"
        values = {
            **self.values,
            "SHARED_DIR": str(assets),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_INSTALLER": str(installer),
            "FAKE_DESTROY_MARKER": str(marker),
        }
        try:
            main.execute("destroy", self.root, runtime, values)
        except RuntimeError:
            # This fixture contains dummy credentials only; expose its failure for debugging.
            self.fail((self.root / "logs/destroy-destroy.log").read_text())
        self.assertTrue(marker.exists())
        self.assertTrue((assets / "private-state").exists())

    def test_script_failure_keeps_output_private(self):
        script = self.root / "release/test-commands.sh"
        script.write_text("echo dummy-test-credential; echo dummy-test-credential >&2; exit 7\n")
        with self.assertRaises(RuntimeError) as raised:
            main.execute("test", self.root, self.root / "install", self.values)
        self.assertNotIn("dummy-test-credential", str(raised.exception))
        self.assertIn("dummy-test-credential", (self.root / "logs/install-test.log").read_text())


class PipelineTests(unittest.TestCase):
    def test_forge_launcher_finds_executable_cluster_ci_script(self):
        from projects.core.ci_entrypoint.run_common import find_script

        project = env.FORGE_HOME / "projects/cluster"
        self.assertEqual(find_script(project, "ci"), project / "orchestration/ci.py")

    def test_two_tasks_bind_internal_storage_and_destroy_runs_in_finally(self):
        workflows = env.FORGE_HOME / "fournos/gitops/base/workflows"
        pipeline = yaml.safe_load(
            (workflows / "pipeline-ibmcloud-create-destroy.yaml").read_text()
        )["spec"]
        task = yaml.safe_load((workflows / "task-forge-step.yaml").read_text())["spec"]
        self.assertEqual([step["name"] for step in pipeline["tasks"]], ["create"])
        self.assertEqual([step["name"] for step in pipeline["finally"]], ["destroy"])
        # Fournos supplies its existing single volumeClaimTemplate workspace.
        self.assertEqual([w["name"] for w in pipeline["workspaces"]], ["artifacts"])
        self.assertTrue(next(w for w in task["workspaces"] if w["name"] == "internal")["optional"])
        for phase, command in (("tasks", "ibmcloud-create"), ("finally", "ibmcloud-destroy")):
            step = pipeline[phase][0]
            self.assertEqual(step["taskRef"]["name"], "forge-step")
            self.assertEqual(
                {w["name"]: (w["workspace"], w["subPath"]) for w in step["workspaces"]},
                {"artifacts": ("artifacts", "public"), "internal": ("artifacts", "internal")},
            )
            self.assertEqual(
                next(p["value"] for p in step["params"] if p["name"] == "job-step"), command
            )
            self.assertNotIn("when", step)
            self.assertNotIn("$(tasks.", json.dumps(step))


if __name__ == "__main__":
    unittest.main()
