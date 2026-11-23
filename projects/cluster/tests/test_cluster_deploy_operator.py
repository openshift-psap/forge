from __future__ import annotations

import copy
import json
import logging
import re
from types import SimpleNamespace

import pytest
import yaml

from projects.cluster.toolbox.cluster_deploy_operator import main as deploy_operator

# Spec: projects/cluster/toolbox/cluster_deploy_operator/packagemanifest.allium

PACKAGE = "authorino-operator"
SOURCE = "redhat-operators"
OTHER_SOURCE = "community-operators"
SOURCE_NS = "openshift-marketplace"
CHANNEL = "stable"


def _manifest(name, catalog, channel=CHANNEL, install_modes=None, marker=""):
    if install_modes is None:
        install_modes = [
            {"type": "OwnNamespace", "supported": True},
            {"type": "SingleNamespace", "supported": True},
            {"type": "MultiNamespace", "supported": False},
            {"type": "AllNamespaces", "supported": True},
        ]
    return {
        "apiVersion": "packages.operators.coreos.com/v1",
        "kind": "PackageManifest",
        "metadata": {
            "name": name,
            "namespace": SOURCE_NS,
            "labels": {"catalog": catalog, "catalog-namespace": SOURCE_NS},
        },
        "status": {
            "catalogSource": catalog,
            "packageName": name,
            "defaultChannel": channel,
            "channels": [
                {
                    "name": channel,
                    "currentCSV": f"{name}.v1.0.0{marker}",
                    "currentCSVDesc": {"installModes": install_modes},
                },
                {
                    "name": "tech-preview",
                    "currentCSV": f"{name}.v1.1.0{marker}",
                    "currentCSVDesc": {
                        "installModes": [{"type": "AllNamespaces", "supported": True}]
                    },
                },
            ],
        },
    }


def _cluster_manifests():
    """All PackageManifests served in the source namespace, across catalogs.

    The same package name is served by two catalogs: the one from OTHER_SOURCE
    must never leak into the artifacts (ArtifactsHoldOnlyTheTargetPackage).
    """
    return [
        _manifest("other-operator", SOURCE),
        _manifest(PACKAGE, OTHER_SOURCE, marker="-community"),
        _manifest(PACKAGE, SOURCE),
        _manifest("yet-another-operator", SOURCE),
    ]


class FakeOc:
    """Fake `shell.run` emulating `oc get packagemanifest -n NS -lcatalog=X -o json`.

    The label selector is applied the way the API server would, so the
    catalog scoping done by the command itself is exercised.
    """

    def __init__(self, manifests=None, success=True, stdout=None):
        self.manifests = manifests if manifests is not None else _cluster_manifests()
        self.success = success
        self.stdout = stdout
        self.commands = []

    def run(self, cmd, *args, **kwargs):
        self.commands.append(cmd)
        if not self.success:
            return SimpleNamespace(success=False, stdout="", stderr="error", returncode=1)
        if self.stdout is not None:
            return SimpleNamespace(success=True, stdout=self.stdout, stderr="", returncode=0)

        ns = re.search(r"-n (\S+)", cmd).group(1)
        catalog_match = re.search(r"-lcatalog=(\S+)", cmd)
        items = [m for m in self.manifests if m["metadata"]["namespace"] == ns]
        if catalog_match:
            catalog = catalog_match.group(1)
            items = [m for m in items if m["metadata"]["labels"].get("catalog") == catalog]
        listing = {"apiVersion": "v1", "kind": "List", "items": copy.deepcopy(items)}
        return SimpleNamespace(success=True, stdout=json.dumps(listing), stderr="", returncode=0)


@pytest.fixture
def args(tmp_path):
    (tmp_path / "src").mkdir()
    return SimpleNamespace(
        package_name=PACKAGE,
        target_namespace="authorino",
        source_name=SOURCE,
        source_namespace=SOURCE_NS,
        channel=CHANNEL,
        install_mode="auto",
        artifact_dir=tmp_path,
    )


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def dsl_log():
    """Capture the DSL logger directly (it does not propagate to caplog)."""
    dsl_logger = logging.getLogger("DSL")
    handler = _ListHandler()
    previous_level = dsl_logger.level
    dsl_logger.addHandler(handler)
    dsl_logger.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        dsl_logger.removeHandler(handler)
        dsl_logger.setLevel(previous_level)


def _install_fake_oc(monkeypatch, fake):
    monkeypatch.setattr(deploy_operator, "shell", SimpleNamespace(run=fake.run))
    return fake


def _assert_is_target_manifest(data):
    """ArtifactsHoldOnlyTheTargetPackage: one manifest, target name, source catalog."""
    assert isinstance(data, dict)
    assert "items" not in data
    assert data["metadata"]["name"] == PACKAGE
    assert data["metadata"]["labels"]["catalog"] == SOURCE
    assert all("community" not in ch["currentCSV"] for ch in data["status"]["channels"])


def _assert_listing_command(cmd, args):
    assert "oc get packagemanifest" in cmd
    assert f"-n {args.source_namespace}" in cmd
    assert f"-lcatalog={args.source_name}" in cmd
    assert "-o json" in cmd


# --- _select_package_manifest --------------------------------------------------


def test_select_package_manifest_returns_first_name_match() -> None:
    first = _manifest(PACKAGE, SOURCE, marker="-first")
    second = _manifest(PACKAGE, SOURCE, marker="-second")
    listing = {"items": [_manifest("other-operator", SOURCE), first, second]}

    assert deploy_operator._select_package_manifest(listing, PACKAGE) is listing["items"][1]


def test_select_package_manifest_returns_none_when_absent() -> None:
    listing = {"items": [_manifest("other-operator", SOURCE)]}

    assert deploy_operator._select_package_manifest(listing, PACKAGE) is None
    assert deploy_operator._select_package_manifest({}, PACKAGE) is None


# --- PackageManifestFound -----------------------------------------------------


def test_wait_for_package_manifest_found_saves_selected_manifest(monkeypatch, args) -> None:
    fake = _install_fake_oc(monkeypatch, FakeOc())
    ctx = SimpleNamespace()

    result = deploy_operator.wait_for_package_manifest.original_func(args, ctx)

    assert isinstance(result, str)
    assert len(fake.commands) == 1
    _assert_listing_command(fake.commands[0], args)

    expected_path = args.artifact_dir / "src" / f"{PACKAGE}-packagemanifest.json"
    assert ctx.package_manifest_file == expected_path
    saved = json.loads(expected_path.read_text())
    _assert_is_target_manifest(saved)
    assert saved == _manifest(PACKAGE, SOURCE)


def test_wait_for_package_manifest_ignores_same_name_from_other_catalog(monkeypatch, args) -> None:
    # The other catalog's same-named manifest comes first in namespace order;
    # the catalog label scope must exclude it.
    manifests = [_manifest(PACKAGE, OTHER_SOURCE, marker="-community"), _manifest(PACKAGE, SOURCE)]
    _install_fake_oc(monkeypatch, FakeOc(manifests=manifests))
    ctx = SimpleNamespace()

    deploy_operator.wait_for_package_manifest.original_func(args, ctx)

    _assert_is_target_manifest(json.loads(ctx.package_manifest_file.read_text()))


# --- PackageManifestNotYetAvailable ------------------------------------------


@pytest.mark.parametrize(
    "fake",
    [
        pytest.param(lambda: FakeOc(success=False), id="listing-fails"),
        pytest.param(lambda: FakeOc(stdout="not json{"), id="listing-unparseable"),
        pytest.param(
            lambda: FakeOc(manifests=[_manifest("other-operator", SOURCE)]),
            id="package-absent-from-catalog",
        ),
        pytest.param(
            lambda: FakeOc(manifests=[_manifest(PACKAGE, OTHER_SOURCE)]),
            id="package-only-in-other-catalog",
        ),
    ],
)
def test_wait_for_package_manifest_not_yet_available_retries(monkeypatch, args, fake) -> None:
    _install_fake_oc(monkeypatch, fake())
    ctx = SimpleNamespace()

    result = deploy_operator.wait_for_package_manifest.original_func(args, ctx)

    assert isinstance(result, tuple)
    assert result[0] is False
    assert isinstance(result[1], str) and result[1]
    # SelectedManifestOnlyAfterSuccessfulLookup: no working file without a found lookup
    assert not hasattr(ctx, "package_manifest_file")
    assert list((args.artifact_dir / "src").iterdir()) == []


def test_wait_for_package_manifest_retry_policy_matches_spec_config() -> None:
    retry_config = deploy_operator.wait_for_package_manifest._task_info["retry_config"]

    assert retry_config["attempts"] == 60  # config.max_lookup_attempts
    assert retry_config["delay"] == 15  # config.lookup_poll_interval


# --- ReadInstallModesFromSelectedManifest (read side only) --------------------


def test_check_install_modes_reads_named_channel_from_selected_manifest(
    monkeypatch, args, dsl_log
) -> None:
    _install_fake_oc(monkeypatch, FakeOc())
    ctx = SimpleNamespace(cluster_wide=None)
    deploy_operator.wait_for_package_manifest.original_func(args, ctx)

    result = deploy_operator.check_install_modes.original_func(args, ctx)

    assert isinstance(result, str)
    messages = [m for m in dsl_log if "supported install modes" in m]
    assert len(messages) == 1
    # modes of the "stable" channel, not "tech-preview" (which only has AllNamespaces)
    assert "'OwnNamespace': True" in messages[0]
    assert "'SingleNamespace': True" in messages[0]
    assert "'MultiNamespace': False" in messages[0]
    assert "'AllNamespaces': True" in messages[0]


def test_check_install_modes_reads_from_saved_file_without_querying_cluster(
    monkeypatch, args, dsl_log
) -> None:
    manifest_file = args.artifact_dir / "src" / f"{PACKAGE}-packagemanifest.json"
    manifest_file.write_text(json.dumps(_manifest(PACKAGE, SOURCE)))
    fake = _install_fake_oc(monkeypatch, FakeOc())
    args.channel = "tech-preview"
    ctx = SimpleNamespace(cluster_wide=None, package_manifest_file=manifest_file)

    deploy_operator.check_install_modes.original_func(args, ctx)

    assert fake.commands == []
    messages = [m for m in dsl_log if "supported install modes" in m]
    assert len(messages) == 1
    assert "{'AllNamespaces': True}" in messages[0]


# --- CapturePackageManifestDiagnostics ---------------------------------------


def test_capture_package_manifest_writes_only_target_package(monkeypatch, args) -> None:
    fake = _install_fake_oc(monkeypatch, FakeOc())
    artifacts_dir = args.artifact_dir / "artifacts"
    assert not artifacts_dir.exists()  # the task creates it

    result = deploy_operator.capture_package_manifest(args, SimpleNamespace())

    assert isinstance(result, str)
    assert len(fake.commands) == 1
    _assert_listing_command(fake.commands[0], args)

    json_file = artifacts_dir / "packagemanifest.json"
    yaml_file = artifacts_dir / "packagemanifest.yaml"
    json_data = json.loads(json_file.read_text())
    yaml_data = yaml.safe_load(yaml_file.read_text())

    _assert_is_target_manifest(json_data)
    _assert_is_target_manifest(yaml_data)
    assert json_data == _manifest(PACKAGE, SOURCE)
    assert yaml_data == json_data
    assert json_file.read_text() == json.dumps(json_data, indent=2)


def test_capture_package_manifest_excludes_same_name_from_other_catalog(monkeypatch, args) -> None:
    manifests = [_manifest(PACKAGE, OTHER_SOURCE, marker="-community"), _manifest(PACKAGE, SOURCE)]
    _install_fake_oc(monkeypatch, FakeOc(manifests=manifests))

    deploy_operator.capture_package_manifest(args, SimpleNamespace())

    artifacts_dir = args.artifact_dir / "artifacts"
    _assert_is_target_manifest(json.loads((artifacts_dir / "packagemanifest.json").read_text()))
    _assert_is_target_manifest(yaml.safe_load((artifacts_dir / "packagemanifest.yaml").read_text()))


def test_capture_package_manifest_works_with_existing_artifacts_dir(monkeypatch, args) -> None:
    (args.artifact_dir / "artifacts").mkdir()
    _install_fake_oc(monkeypatch, FakeOc())

    deploy_operator.capture_package_manifest(args, SimpleNamespace())

    assert (args.artifact_dir / "artifacts" / "packagemanifest.json").is_file()
    assert (args.artifact_dir / "artifacts" / "packagemanifest.yaml").is_file()


@pytest.mark.parametrize(
    "fake",
    [
        pytest.param(
            lambda: FakeOc(manifests=[_manifest("other-operator", SOURCE)]),
            id="package-absent-from-catalog",
        ),
        pytest.param(
            lambda: FakeOc(manifests=[_manifest(PACKAGE, OTHER_SOURCE)]),
            id="package-only-in-other-catalog",
        ),
        pytest.param(lambda: FakeOc(success=False), id="listing-fails"),
        pytest.param(lambda: FakeOc(stdout="not json{"), id="listing-unparseable"),
    ],
)
def test_capture_package_manifest_writes_nothing_and_never_raises(monkeypatch, args, fake) -> None:
    _install_fake_oc(monkeypatch, fake())

    result = deploy_operator.capture_package_manifest(args, SimpleNamespace())

    assert isinstance(result, str) and result
    artifacts_dir = args.artifact_dir / "artifacts"
    assert not (artifacts_dir / "packagemanifest.json").exists()
    assert not (artifacts_dir / "packagemanifest.yaml").exists()
