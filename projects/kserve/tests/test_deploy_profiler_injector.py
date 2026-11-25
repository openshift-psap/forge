"""Tests for the deploy_profiler_injector toolbox command."""

from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from projects.core.dsl import shell as dsl_shell
from projects.kserve.toolbox.deploy_profiler_injector import main as deploy

OWNERSHIP_LABELS = {
    "app.kubernetes.io/managed-by": "forge",
    "forge.openshift.io/project": "llm_d",
}

ARGS = SimpleNamespace(
    namespace="ns-target",
    image="quay.io/mimehta/vllmprofiler",
    target_labels="vllm-profiler/enabled=true",
    profiling_ranges="100-200",
    inject_env_name="PYTHONPATH",
    inject_env_value="/home/vllm/profiler",
    labels=OWNERSHIP_LABELS,
)


class FakeShell:
    """Captures shell.run calls; simulates a cluster with nothing deployed."""

    def __init__(self):
        self.calls: list[SimpleNamespace] = []

    def __call__(self, command, check=True, input_text=None, **_kwargs):
        self.calls.append(SimpleNamespace(command=command, input_text=input_text, check=check))

        if command.startswith("oc get namespace "):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if command.startswith("oc get serviceaccount "):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if command.startswith("oc get secret "):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if command.startswith("oc get configmap "):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if command.startswith("oc get mutatingwebhookconfiguration"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if "oc get pods" in command and "app=env-injector" in command:
            return SimpleNamespace(returncode=0, stdout="Running", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def applied_manifests(self) -> list[dict]:
        return [
            yaml.safe_load(call.input_text)
            for call in self.calls
            if call.input_text and call.command.startswith("oc apply")
        ]

    def commands(self) -> list[str]:
        return [call.command for call in self.calls]


def _run_all_tasks(fake: FakeShell) -> SimpleNamespace:
    context = SimpleNamespace()
    with patch.object(dsl_shell, "run", side_effect=fake):
        for task in [
            deploy.ensure_injector_namespace,
            deploy.ensure_service_account,
            deploy.ensure_tls_certificate,
            deploy.deploy_webhook_configuration,
            deploy.deploy_injector_deployment,
            deploy.deploy_profiler_configmap,
            deploy.wait_for_injector_pod,
        ]:
            task(ARGS, context)
    return context


def test_full_deploy_on_clean_cluster() -> None:
    fake = FakeShell()
    _run_all_tasks(fake)

    manifests = fake.applied_manifests()
    by_kind = {(m["kind"], m["metadata"]["name"]): m for m in manifests}

    webhook = by_kind[("MutatingWebhookConfiguration", "env-injector-webhook")]
    assert webhook["webhooks"][0]["failurePolicy"] == "Ignore"
    assert webhook["webhooks"][0]["rules"] == [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE"],
            "resources": ["pods"],
            "scope": "*",
        }
    ]
    assert webhook["webhooks"][0]["clientConfig"]["service"] == {
        "name": "env-injector",
        "namespace": "vllm-profiler",
        "path": "/mutate",
        "port": 443,
    }
    assert len(webhook["webhooks"][0]["clientConfig"]["caBundle"]) >= deploy.MIN_CA_BUNDLE_LENGTH

    deployment = by_kind[("Deployment", "env-injector")]
    env = {
        e["name"]: e["value"]
        for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["TARGET_NAMESPACE"] == "ns-target"
    assert env["TARGET_LABELS"] == "vllm-profiler/enabled=true"
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == ARGS.image

    assert ("Service", "env-injector") in by_kind

    configmap = by_kind[("ConfigMap", "env-injector-files")]
    assert configmap["metadata"]["namespace"] == "ns-target"
    assert "class ProfilerConfig" in configmap["data"]["sitecustomize.py"]
    assert 'profiling_ranges: "100-200"' in configmap["data"]["profiler_config.yaml"]

    # Ownership labels on every created resource
    for kind, name in [
        ("MutatingWebhookConfiguration", "env-injector-webhook"),
        ("Deployment", "env-injector"),
        ("Service", "env-injector"),
        ("ConfigMap", "env-injector-files"),
    ]:
        assert by_kind[(kind, name)]["metadata"]["labels"] == OWNERSHIP_LABELS

    # Namespace and ServiceAccount are created via labelled manifests
    # (atomic: no unlabelled crash window); the TLS secret is created by
    # oc create secret tls and labelled afterwards.
    namespace_manifest = by_kind[("Namespace", "vllm-profiler")]
    assert namespace_manifest["metadata"]["labels"] == OWNERSHIP_LABELS
    sa_manifest = by_kind[("ServiceAccount", "env-injector")]
    assert sa_manifest["metadata"]["namespace"] == "vllm-profiler"
    assert sa_manifest["metadata"]["labels"] == OWNERSHIP_LABELS
    label_commands = [cmd for cmd in fake.commands() if cmd.startswith("oc label")]
    assert any("oc label secret env-injector-certs" in cmd for cmd in label_commands)


def test_tls_certificate_generation_and_reuse() -> None:
    fake = FakeShell()
    _run_all_tasks(fake)
    assert any("oc create secret tls" in cmd for cmd in fake.commands())

    # Second run: secret and valid webhook CA bundle already exist.
    class ReusingShell(FakeShell):
        def __call__(self, command, check=True, input_text=None, **_kwargs):
            result = super().__call__(command, check, input_text, **_kwargs)
            if command.startswith("oc get secret "):
                return SimpleNamespace(returncode=0, stdout="secret/env-injector-certs", stderr="")
            if command.startswith("oc get mutatingwebhookconfiguration"):
                ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----" + b"x" * 200).decode()
                return SimpleNamespace(returncode=0, stdout=ca, stderr="")
            return result

    fake2 = ReusingShell()
    _run_all_tasks(fake2)
    assert not any("oc create secret tls" in cmd for cmd in fake2.commands())


def test_tls_private_key_never_reaches_artifacts() -> None:
    fake = FakeShell()
    _run_all_tasks(fake)

    for call in fake.calls:
        if "tls.key" in str(call.command):
            assert "artifacts" not in str(call.command).lower()
        if call.input_text:
            assert "PRIVATE KEY" not in call.input_text


def test_no_uninterpolated_placeholders() -> None:
    source = Path(deploy.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "{args." not in node.value, f"line {node.lineno}: {node.value!r}"
            assert "{context." not in node.value, f"line {node.lineno}: {node.value!r}"


def test_wait_for_injector_pod_retries() -> None:
    # The pod needs time to pull and schedule; the wait must poll, not
    # fail on the first Pending observation (AGENTS.md: no single-shot waits).
    assert deploy.wait_for_injector_pod._retry_config["attempts"] >= 30
    assert deploy.wait_for_injector_pod._retry_config["delay"] >= 10


class TeardownShell:
    """Simulates cluster state for teardown_profiler_injector."""

    def __init__(self, resource_labels: dict[str, str | None]):
        self.resource_labels = resource_labels  # resource key -> labels (None = absent)
        self.deleted: list[str] = []

    def __call__(self, command, check=True, input_text=None, **_kwargs):
        if command.startswith("oc get "):
            parts = command.split()
            resource = parts[2]
            name = parts[3]
            key = f"{resource}/{name}"
            labels = self.resource_labels.get(key, None)
            if labels is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="NotFound")
            return SimpleNamespace(returncode=0, stdout=json.dumps(labels), stderr="")

        if command.startswith("oc delete"):
            self.deleted.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _run_teardown(fake: TeardownShell) -> None:
    from projects.kserve.toolbox.teardown_profiler_injector import main as teardown

    args = SimpleNamespace(namespace="ns-target", labels=OWNERSHIP_LABELS)
    with patch.object(dsl_shell, "run", side_effect=fake):
        for task in [
            teardown.remove_webhook_configuration,
            teardown.remove_injector_namespace,
            teardown.remove_profiler_configmap,
            teardown.remove_orphaned_tls_secret,
        ]:
            task(args, SimpleNamespace())


def test_teardown_removes_owned_resources() -> None:
    fake = TeardownShell(
        {
            "mutatingwebhookconfiguration/env-injector-webhook": OWNERSHIP_LABELS,
            "namespace/vllm-profiler": OWNERSHIP_LABELS,
            "configmap/env-injector-files": OWNERSHIP_LABELS,
        }
    )
    _run_teardown(fake)

    deleted = " ".join(fake.deleted)
    assert "mutatingwebhookconfiguration env-injector-webhook" in deleted
    assert "namespace vllm-profiler" in deleted
    assert "configmap env-injector-files" in deleted
    # the webhook goes first so pod creation stops hitting it immediately
    assert fake.deleted[0].startswith("oc delete mutatingwebhookconfiguration")


def test_teardown_never_removes_foreign_resources() -> None:
    foreign_labels = {"somebody": "else"}
    fake = TeardownShell(
        {
            "mutatingwebhookconfiguration/env-injector-webhook": foreign_labels,
            "namespace/vllm-profiler": foreign_labels,
            "configmap/env-injector-files": foreign_labels,
            "secret/env-injector-certs": foreign_labels,
        }
    )
    _run_teardown(fake)
    assert fake.deleted == []


def test_teardown_reports_absent_resources() -> None:
    fake = TeardownShell({})
    _run_teardown(fake)
    assert fake.deleted == []


def test_teardown_removes_orphaned_owned_secret_in_foreign_namespace() -> None:
    fake = TeardownShell(
        {
            "mutatingwebhookconfiguration/env-injector-webhook": None,
            "namespace/vllm-profiler": {"pre-existing": "true"},
            "configmap/env-injector-files": None,
            "secret/env-injector-certs": OWNERSHIP_LABELS,
        }
    )
    _run_teardown(fake)
    assert len(fake.deleted) == 1
    assert "secret env-injector-certs" in fake.deleted[0]


def test_teardown_requires_ownership_labels() -> None:
    from projects.kserve.toolbox.teardown_profiler_injector import main as teardown

    with pytest.raises(ValueError, match="ownership labels"):
        teardown.run(namespace="ns-target", labels=None)


def test_deploy_reuses_foreign_configmap_without_relabeling() -> None:
    fake = FakeShell()

    # ConfigMap already exists with foreign labels
    def configmap_get(command, check=True, input_text=None, **_kwargs):
        if command.startswith("oc get configmap "):
            return SimpleNamespace(returncode=0, stdout='{"custom":"true"}', stderr="")
        return fake(command, check=check, input_text=input_text, **_kwargs)

    with patch.object(dsl_shell, "run", side_effect=configmap_get):
        deploy.deploy_profiler_configmap(ARGS, SimpleNamespace())

    applied = [call for call in fake.calls if call.input_text and "ConfigMap" in call.input_text]
    assert applied == []


def test_label_commands_use_separate_arguments() -> None:
    # oc label rejects a single comma-joined spec ("invalid label spec");
    # each key=value must be its own argument. Caught live on the first
    # clean-slate deploy after the mocks had hidden it. Only the TLS
    # secret still uses oc label; ns/SA are labelled via their manifests.
    fake = FakeShell()
    _run_all_tasks(fake)
    label_commands = [cmd for cmd in fake.commands() if cmd.startswith("oc label")]
    assert label_commands, "expected the secret label command on the create path"
    for cmd in label_commands:
        spec = cmd.split(" ", 4)[-1]  # everything after "oc label <type> <name> [-n ns]"
        assert "," not in spec, f"comma-joined label spec: {cmd}"
        assert " " in spec, f"expected multiple label args: {cmd}"
