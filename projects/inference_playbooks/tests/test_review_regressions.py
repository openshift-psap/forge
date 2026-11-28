"""PR review regressions across the hosted execution boundary."""

import json
from contextlib import nullcontext

import pytest

from projects.inference_playbooks.orchestration import private_images as images
from projects.inference_playbooks.orchestration import test_phase
from projects.kserve.toolbox.deploy_lws.main import _select_operator_service


@pytest.mark.parametrize("concurrency", [1, [1, 5]])
def test_aiperf_job_names_retain_workload_and_fit_toolbox_limit(monkeypatch, concurrency):
    from projects.aiperf.toolbox.run_aiperf_benchmark import main as command

    calls = []
    monkeypatch.setattr(test_phase.env, "NextArtifactDir", lambda *a: nullcontext())
    monkeypatch.setattr(test_phase, "create_test_metadata", lambda *a, **kw: None)
    monkeypatch.setattr(command, "run", lambda **kw: calls.append(kw))
    test_phase._run_aiperf_workload(
        "http://model.example:8000",
        "fixture",
        "very-long-resource-name-" * 3,
        "model",
        "model",
        "aiperf-agentx-128k",
        {"concurrency": concurrency},
        {},
    )
    assert len(calls) == (2 if isinstance(concurrency, list) else 1)
    assert all(len(call["name"]) <= 55 for call in calls)
    assert all("aiperf-agentx-128k" in call["name"] for call in calls)
    assert len({call["name"] for call in calls}) == len(calls)


def test_lws_discovery_ignores_portless_operator_service():
    peer = {"metadata": {"name": "example"}, "spec": {}}
    leader = {"metadata": {"name": "example-leader"}, "spec": {"ports": [{"port": 8000}]}}
    assert _select_operator_service([peer], "example") is None
    assert _select_operator_service([peer, leader], "example") == leader


@pytest.mark.parametrize("leak", [None, "private-team/runtime", "sha256:" + "c" * 64])
def test_shared_registry_publication_keeps_private_identifier_guards(tmp_path, monkeypatch, leak):
    if "ARTIFACT_DIR" in vars(images.env):
        monkeypatch.delattr(images.env, "ARTIFACT_DIR")
    private = images.PrivateImage(
        {"image_visibility": "private", "image_env": "RC_IMAGE"},
        "quay.io/private-team/runtime:rc7",
        public_images=["quay.io/public/harness:v1"],
    )
    private.tokens.add("sha256:" + "c" * 64)
    destination = tmp_path / "published"

    def publish():
        with images.env.TempArtifactDir(destination), private.artifact_scope():
            path = images.env.ARTIFACT_DIR / "record.json"
            path.write_text(json.dumps({"harness": "quay.io/public/harness:v1", "value": leak}))
            private.publish(path)

    if leak:
        with pytest.raises(ValueError, match="publication blocked"):
            publish()
        assert not (destination / "record.json").exists()
    else:
        publish()
        assert (destination / "record.json").exists()


def test_private_host_and_different_port_remain_protected():
    private = images.PrivateImage(
        {"image_visibility": "private", "image_env": "RC_IMAGE"},
        "restricted.example.org:5000/team/runtime:rc7",
        public_images=["restricted.example.org/public/harness:v1"],
    )
    with pytest.raises(ValueError, match="private image identifier"):
        private.assert_public({"diagnostic": "restricted.example.org"})
