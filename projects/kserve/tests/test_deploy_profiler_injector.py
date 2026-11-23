"""Tests for the deploy_profiler_injector toolbox command."""

from __future__ import annotations

import ast
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from projects.core.dsl import shell as dsl_shell
from projects.kserve.toolbox.deploy_profiler_injector import main as deploy

ARGS = SimpleNamespace(
    namespace="ns-target",
    injector_namespace="vllm-profiler",
    image="quay.io/mimehta/vllmprofiler",
    target_labels="vllm-profiler/enabled=true",
    profiling_ranges="100-200",
    inject_env_name="PYTHONPATH",
    inject_env_value="/home/vllm/profiler",
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
