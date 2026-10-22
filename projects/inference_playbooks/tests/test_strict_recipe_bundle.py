import copy
import hashlib
import importlib
import json
import logging
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from projects.inference_playbooks.orchestration import manifest_bundle as contract
from projects.inference_playbooks.orchestration import test_phase
from projects.inference_playbooks.toolbox.wait_recipe import main as waiter


def _manifest(kind="Deployment"):
    pod = {
        "metadata": {"labels": {"app": "authored"}},
        "spec": {
            "securityContext": {"runAsUser": 1000770000},
            "containers": [
                {
                    "name": "server",
                    "image": "quay.io/example/server:pinned",
                    "command": ["vllm"],
                    "args": ["serve", "example/model", "--port", "8000"],
                    "env": [
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {"name": "external-auth", "key": "token"}
                            },
                        }
                    ],
                    "resources": {"limits": {"nvidia.com/gpu": 1, "rdma/ib": 1}},
                    "ports": [{"name": "http", "containerPort": 8000}],
                    "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}},
                    "volumeMounts": [{"name": "model", "mountPath": "/models"}],
                }
            ],
            "volumes": [
                {"name": "model", "persistentVolumeClaim": {"claimName": "existing-model"}}
            ],
        },
    }
    versions = {key: value[0] for key, value in contract.RESOURCE_TYPES.items()}
    spec = {"replicas": 1, "selector": {"matchLabels": {"app": "authored"}}, "template": pod}
    if kind == "LeaderWorkerSet":
        spec = {
            "replicas": 1,
            "leaderWorkerTemplate": {
                "size": 2,
                "leaderTemplate": copy.deepcopy(pod),
                "workerTemplate": copy.deepcopy(pod),
            },
        }
    elif kind == "InferenceService":
        spec = {
            "predictor": {
                "model": {"runtime": "authored-runtime", "storageUri": "hf://example/model"}
            }
        }
    elif kind == "LLMInferenceService":
        spec = {
            "model": {"name": "example/model", "uri": "hf://example/model"},
            "template": pod["spec"],
        }
    elif kind == "ServingRuntime":
        spec = {"containers": pod["spec"]["containers"]}
    return {
        "apiVersion": versions[kind],
        "kind": kind,
        "metadata": {"name": "authored-runtime" if kind == "ServingRuntime" else "authored"},
        "spec": spec,
    }


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """All Git and cluster access is mocked; no fixture commits or cluster commands."""
    root = tmp_path / "checkout"
    recipe_path = root / "models/model/recipes/recipe/recipe.yaml"
    folder = recipe_path.parent / "manifests/vllm-v1"
    folder.mkdir(parents=True)
    authored = {
        "schema_version": 4,
        "recipe_id": "synthetic",
        "model_id": "model",
        "platforms": [{"stack": "vllm", "version": "v1", "overrides": "platforms/vllm-v1.yaml"}],
        "hardware_profile": "hardware.yaml",
        "serving": {"model": "example/model", "port": 8000},
    }
    recipe_path.write_text(yaml.safe_dump(authored))
    (root / "models/model/model.yaml").write_text(
        "model_id: model\nhuggingface_id: example/model\n"
    )
    (root / "hardware.yaml").write_text("accelerators: {vendor: nvidia, model: H200}\n")
    primary_path = folder / "b-server.yaml"
    primary_path.write_text(yaml.safe_dump(_manifest()))
    snapshots = {}

    def snapshot():
        snapshots.clear()
        snapshots.update(
            {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
        )

    snapshot()
    monkeypatch.setattr(
        contract, "_revision", lambda directory: "a" * 40 if directory == root else "b" * 40
    )
    monkeypatch.setattr(contract, "_git_bytes", lambda _root, _revision, path: snapshots[path])
    monkeypatch.setattr(
        contract,
        "_git_artifact_paths",
        lambda _root, _revision, directory: sorted(
            root / path
            for path in snapshots
            if (root / path).parent == directory and Path(path).suffix in {".yaml", ".yml"}
        ),
    )
    recipe = {
        **authored,
        "recipe_path": recipe_path,
        "repository_path": root,
        "model_name": "example/model",
        "platform": {"version": "v1"},
    }
    monkeypatch.setattr(
        test_phase.config,
        "project",
        SimpleNamespace(
            get_config=lambda key: (
                primary_path.relative_to(recipe_path.parent).as_posix()
                if key == "inference_playbooks.manifest_path"
                else None
            )
        ),
    )
    return SimpleNamespace(
        root=root,
        recipe=recipe,
        path=primary_path,
        folder=folder,
        snapshot=snapshot,
        snapshots=snapshots,
    )


@pytest.mark.parametrize("kind", ["Service", "Secret", "ConfigMap", "List", "DeploymentList"])
def test_rejects_unsupported_documents_without_payload(kind):
    rejected = {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": "authored"},
        "data": {"token": "do-not-print"},
        "items": [{"kind": "Service"}],
    }
    with pytest.raises(ValueError) as error:
        contract.classify_documents([_manifest(), rejected])
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"kind: Deployment\nkind: Service\n",
        b"spec: {env: {TOKEN: do-not-print, TOKEN: changed}}",
        b"spec: [do-not-print",
        b"spec: &cycle {again: *cycle}",
        b"spec: {date: 2026-10-22}",
        b"spec: {.nan: bad}",
        b"\xff",
    ],
)
def test_yaml_rejection_is_duplicate_safe_and_private(raw):
    with pytest.raises(ValueError) as error:
        contract.load_documents(raw)
    assert "do-not-print" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        3,
        {},
        {"kind": []},
        {"kind": "Deployment", "apiVersion": "bad", "metadata": {"name": "authored"}},
    ],
)
def test_rejects_malformed_documents(bad):
    with pytest.raises(ValueError):
        contract.classify_documents([bad])


@pytest.mark.parametrize("namespace", [None, "", "elsewhere", 123])
def test_explicit_namespace_is_never_repaired(namespace):
    source = _manifest()
    source["metadata"]["namespace"] = namespace
    with pytest.raises(ValueError, match="namespace"):
        contract.normalized_documents([source], "target")


def test_namespace_only_placement_and_identity_collision():
    source = _manifest()
    submitted = contract.normalized_documents([source], "target")
    assert source["metadata"] == {"name": "authored"}
    assert submitted[0]["spec"] == source["spec"]
    contract.assert_submission([source], submitted, "target")
    source["metadata"]["namespace"] = "target"
    assert contract.normalized_documents([source], "target") == submitted
    runtime = _manifest("ServingRuntime")
    with pytest.raises(ValueError, match="Duplicate"):
        contract.normalized_documents(
            [_manifest("InferenceService"), runtime, copy.deepcopy(runtime)], "target"
        )


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "labels",
        "selector",
        "command",
        "args",
        "env",
        "security",
        "resources",
        "storage",
        "probe",
        "extra",
        "missing",
        "scalar-type",
        "list-order",
    ],
)
def test_whole_document_comparison_rejects_mutations(field):
    source = [_manifest()]
    submitted = contract.normalized_documents(source, "target")
    primary = submitted[0]
    pod = primary["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if field == "name":
        primary["metadata"]["name"] = "renamed"
    elif field == "labels":
        primary["spec"]["template"]["metadata"]["labels"]["forge.openshift.io/run"] = "random"
    elif field == "selector":
        primary["spec"]["selector"]["matchLabels"]["app"] = "changed"
    elif field in {"command", "args", "env"}:
        container[field] = []
    elif field == "security":
        del pod["securityContext"]["runAsUser"]
    elif field == "resources":
        container["resources"]["limits"]["rdma/ib"] = 2
    elif field == "storage":
        pod["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other-pvc"
    elif field == "probe":
        container["readinessProbe"]["initialDelaySeconds"] = 10
    elif field == "extra":
        submitted.append(_manifest("ServingRuntime"))
    elif field == "missing":
        submitted.clear()
    elif field == "scalar-type":
        primary["spec"]["replicas"] = True  # True == 1 in Python; must still fail.
    else:
        container["args"].reverse()
    with pytest.raises(ValueError, match="differs"):
        contract.assert_submission(source, submitted, "target")


def test_selection_captures_every_platform_file_and_document(checkout):
    checkout.path.write_text(yaml.safe_dump(_manifest("InferenceService")))
    runtime_path = checkout.folder / "a-runtime.yml"
    runtime_path.write_text("---\n" + yaml.safe_dump(_manifest("ServingRuntime")) + "---\n")
    # Neither another platform nor nested files are customer artifacts for this platform.
    nested = checkout.folder / "nested"
    nested.mkdir()
    (nested / "unsupported.yaml").write_text("kind: Service")
    other = checkout.folder.parent / "rhoai-3.5"
    other.mkdir()
    (other / "unselected.yaml").write_text("kind: Service")
    checkout.snapshot()
    bundle = test_phase._load_manifest_override(
        checkout.root,
        str(checkout.path.relative_to(checkout.recipe["recipe_path"].parent)),
        checkout.recipe,
    )
    assert [doc["kind"] for doc in bundle["documents"]] == ["ServingRuntime", "InferenceService"]
    assert [Path(item["path"]).name for item in bundle["source_files"]] == [
        "a-runtime.yml",
        "b-server.yaml",
    ]
    assert [item["document_count"] for item in bundle["source_files"]] == [1, 1]


@pytest.mark.parametrize("loader", ["override", "recipe"])
@pytest.mark.parametrize("kind", ["Service", "Secret", "ConfigMap", "List"])
def test_all_launch_loaders_reject_unselected_sibling_resources(checkout, loader, kind):
    (checkout.folder / "companion.yml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": kind,
                "metadata": {"name": "authored"},
                "data": {"token": "do-not-publish"},
            }
        )
    )
    checkout.snapshot()
    with pytest.raises(ValueError, match="Unsupported"):
        if loader == "override":
            test_phase._load_manifest_override(
                checkout.root,
                str(checkout.path.relative_to(checkout.recipe["recipe_path"].parent)),
                checkout.recipe,
            )
        else:
            test_phase._manifest_bundle_from_recipe(checkout.recipe)
    assert not (checkout.root / "artifacts").exists()


def test_ambiguous_platform_requires_selection(checkout):
    recipe_path = checkout.recipe["recipe_path"]
    authored = yaml.safe_load(recipe_path.read_bytes())
    authored["platforms"].append({"stack": "rhoai", "version": "3.5"})
    recipe_path.write_text(yaml.safe_dump(authored))
    checkout.snapshot()
    with pytest.raises(ValueError, match="explicit manifest_path"):
        contract.load_source_bundle(checkout.root, checkout.recipe)
    assert (
        contract.load_source_bundle(checkout.root, checkout.recipe, checkout.path)["platform"][
            "stack"
        ]
        == "vllm"
    )


@pytest.mark.parametrize(
    "change", ["modified", "missing", "untracked", "symlink", "empty", "arbitrary"]
)
def test_noncanonical_or_incomplete_source_fails(checkout, change):
    selection = checkout.path
    if change == "modified":
        checkout.path.write_text(yaml.safe_dump(_manifest("LeaderWorkerSet")))
    elif change == "missing":
        checkout.path.unlink()
    elif change == "untracked":
        (checkout.folder / "extra.yaml").write_text(yaml.safe_dump(_manifest("ServingRuntime")))
    elif change == "symlink":
        alias = checkout.folder / "alias.yml"
        alias.symlink_to(checkout.path)
        checkout.snapshot()
    elif change == "empty":
        checkout.path.write_text("---\n# empty\n")
        checkout.snapshot()
    else:
        selection = checkout.recipe["recipe_path"].parent / "raw.yaml"
        selection.write_bytes(checkout.path.read_bytes())
    with pytest.raises(ValueError):
        contract.load_source_bundle(checkout.root, checkout.recipe, selection)


def test_artifact_contract_and_final_byte_boundary(checkout, tmp_path):
    bundle = contract.load_source_bundle(checkout.root, checkout.recipe, checkout.path)
    submitted = contract.normalized_documents(bundle["documents"], "target")
    src = tmp_path / "src"
    metadata = contract.write_bundle_artifacts(bundle, submitted, "target", src, Path("/forge"))
    assert {path.name for path in src.iterdir()} == {
        "source-bundle.yaml",
        "submitted-bundle.yaml",
        "bundle-metadata.yaml",
    }
    assert metadata["contract_version"] == 1
    assert metadata["recipe_revision"] == "a" * 40
    assert metadata["forge_revision"] == "b" * 40
    assert metadata["recipe_path"] == "models/model/recipes/recipe/recipe.yaml"
    assert (
        metadata["source_files"][0]["sha256"]
        == hashlib.sha256(checkout.path.read_bytes()).hexdigest()
    )
    assert contract.checked_submission(src, "target", metadata) == submitted
    with (src / "submitted-bundle.yaml").open("a") as handle:
        handle.write("\n# tampered bytes\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        contract.checked_submission(src, "target", metadata)


@pytest.fixture
def launch(checkout, monkeypatch, tmp_path):
    actions = []
    live = {}
    namespace = "target"
    artifact_dir = tmp_path / "run"
    values = {
        "inference_playbooks.recipe": "model/recipe",
        "inference_playbooks.manifest_path": checkout.path.relative_to(
            checkout.recipe["recipe_path"].parent
        ).as_posix(),
        "inference_playbooks.namespace": namespace,
        "inference_playbooks.workload": "profile1",
        "inference_playbooks.lws_ready_timeout_seconds": 90,
        "inference_playbooks.readiness_timeout_seconds": 80,
    }
    monkeypatch.setenv("KUBECONFIG", "/not-used")
    monkeypatch.setenv("CLUSTERLESS_MODE", "false")
    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", artifact_dir, raising=False)
    monkeypatch.setattr(
        test_phase.config, "project", SimpleNamespace(get_config=lambda key: values.get(key))
    )

    def get(resource, *, name=None, **kwargs):
        if resource == "namespaces":
            return {"metadata": {"name": namespace}}
        return live.get((resource, name))

    def oc(*args, **kwargs):
        actions.append((args, kwargs))
        assert args[0] in {"create", "delete"}
        body = json.loads(kwargs["input_text"])
        if args[0] == "delete":
            assert body["preconditions"]["uid"].startswith("created-")
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        assert "--raw=" in args[1] and args[-2:] == ("-f", "-")
        assert body["kind"] not in {"Service", "Secret"}
        body["metadata"]["uid"] = f"created-{body['kind']}"
        body["metadata"]["resourceVersion"] = "admitted"
        live[(test_phase._resource_type(body), body["metadata"]["name"])] = body
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(test_phase, "oc_get_json", get)
    monkeypatch.setattr(test_phase, "oc", oc)
    monkeypatch.setattr(
        test_phase.wait_recipe,
        "run",
        lambda **kwargs: actions.append(("wait", kwargs)) or "http://10.0.0.1:8000",
    )
    monkeypatch.setattr(
        test_phase, "_capture_created_recipe", lambda *args: actions.append(("capture", args))
    )
    monkeypatch.setattr(
        test_phase, "_run_workload", lambda *args, **kwargs: actions.append(("benchmark", args))
    )
    return SimpleNamespace(
        actions=actions, live=live, values=values, src=artifact_dir / "src", checkout=checkout
    )


@pytest.mark.parametrize(
    "kind", ["Deployment", "LeaderWorkerSet", "InferenceService", "LLMInferenceService"]
)
def test_launch_preserves_every_authored_field_and_owns_cleanup(launch, kind):
    source = _manifest(kind)
    launch.checkout.path.write_text(yaml.safe_dump(source))
    if kind == "InferenceService":
        (launch.checkout.folder / "a-runtime.yml").write_text(
            yaml.safe_dump(_manifest("ServingRuntime"))
        )
    launch.checkout.snapshot()
    recipe = launch.checkout.recipe
    # Even old cache hints must not invoke model preparation or storage repair.
    recipe["model_cache"] = {"volume_name": "model"}
    test_phase._launch_recipe("synthetic", recipe)
    raw_calls = [(args, kwargs) for args, kwargs in launch.actions if isinstance(args, tuple)]
    submitted = list(yaml.safe_load_all((launch.src / "submitted-bundle.yaml").read_bytes()))
    created = [
        json.loads(kwargs["input_text"]) for args, kwargs in raw_calls if args[0] == "create"
    ]
    assert created == submitted
    primary = next(doc for doc in submitted if doc["kind"] == kind)
    expected = copy.deepcopy(source)
    expected["metadata"]["namespace"] = "target"
    assert primary == expected
    assert any(action[0] == "benchmark" for action in launch.actions)
    deletions = [(args, kwargs) for args, kwargs in raw_calls if args[0] == "delete"]
    assert len(deletions) == len(created)
    assert all("/services/" not in args[1] for args, _kwargs in deletions)
    assert all(
        json.loads(kwargs["input_text"])["preconditions"]["uid"].startswith("created-")
        for _args, kwargs in deletions
    )
    assert len(list(launch.src.glob("*.yaml"))) == 3 + len(created)


def test_conflict_checks_all_objects_before_any_create(launch):
    launch.checkout.path.write_text(yaml.safe_dump(_manifest("InferenceService")))
    (launch.checkout.folder / "a-runtime.yml").write_text(
        yaml.safe_dump(_manifest("ServingRuntime"))
    )
    launch.checkout.snapshot()
    runtime = _manifest("ServingRuntime")
    launch.live[(test_phase._resource_type(runtime), runtime["metadata"]["name"])] = {
        "metadata": {"uid": "preexisting"}
    }
    with pytest.raises(RuntimeError, match="conflict"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert not any(isinstance(args, tuple) for args, _kwargs in launch.actions)


def test_final_tamper_fails_before_any_create(launch, monkeypatch):
    writer = test_phase.write_bundle_artifacts

    def tamper(*args):
        metadata = writer(*args)
        (launch.src / "submitted-bundle.yaml").write_text(
            yaml.safe_dump(_manifest("LeaderWorkerSet"))
        )
        return metadata

    monkeypatch.setattr(test_phase, "write_bundle_artifacts", tamper)
    with pytest.raises(ValueError, match="hash mismatch"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert not any(isinstance(args, tuple) for args, _kwargs in launch.actions)


def test_race_conflict_does_not_delete_racing_resource(launch, monkeypatch):
    def conflict(*args, **kwargs):
        launch.actions.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="AlreadyExists")

    monkeypatch.setattr(test_phase, "oc", conflict)
    with pytest.raises(RuntimeError, match="creation failed"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert [args[0] for args, _kwargs in launch.actions if isinstance(args, tuple)] == ["create"]


def test_partial_creation_cleans_only_proven_created_companion(launch, monkeypatch):
    launch.checkout.path.write_text(yaml.safe_dump(_manifest("InferenceService")))
    (launch.checkout.folder / "a-runtime.yml").write_text(
        yaml.safe_dump(_manifest("ServingRuntime"))
    )
    launch.checkout.snapshot()
    original = test_phase.oc

    def fail_primary(*args, **kwargs):
        body = json.loads(kwargs["input_text"])
        if args[0] == "create" and body["kind"] == "InferenceService":
            return SimpleNamespace(returncode=1, stdout="", stderr="admission failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(test_phase, "oc", fail_primary)
    with pytest.raises(RuntimeError, match="creation failed"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    deletes = [
        args for args, _kwargs in launch.actions if isinstance(args, tuple) and args[0] == "delete"
    ]
    assert len(deletes) == 1 and "/servingruntimes/authored-runtime" in deletes[0][1]


def test_replacement_uid_is_not_deleted(monkeypatch):
    created = _manifest()
    created["metadata"].update(namespace="target", uid="owned")
    monkeypatch.setattr(
        test_phase, "oc_get_json", lambda *args, **kwargs: {"metadata": {"uid": "replacement"}}
    )
    monkeypatch.setattr(
        test_phase, "oc", lambda *args, **kwargs: pytest.fail("must not delete replacement")
    )
    with pytest.raises(RuntimeError, match="UID changed"):
        test_phase._delete_created_resources("target", [created])


def _owned(
    kind,
    name,
    uid,
    owner_uid="root",
    owner_kind="LeaderWorkerSet",
    owner_version="leaderworkerset.x-k8s.io/v1",
):
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "target",
            "uid": uid,
            "ownerReferences": [
                {
                    "apiVersion": owner_version,
                    "kind": owner_kind,
                    "name": "authored",
                    "uid": owner_uid,
                    "controller": True,
                }
            ],
        },
    }


def test_endpoint_without_http_service_uses_only_owned_ready_leader(monkeypatch, tmp_path):
    primary = _manifest("LeaderWorkerSet")
    primary["metadata"].update(namespace="target", uid="root")
    leader = _owned("Pod", "leader", "pod-uid")
    leader["metadata"]["labels"] = {"leaderworkerset.sigs.k8s.io/worker-index": "0"}
    leader["status"] = {"podIP": "10.0.0.5", "conditions": [{"type": "Ready", "status": "True"}]}
    worker = copy.deepcopy(leader)
    worker["metadata"].update(
        name="worker", labels={"leaderworkerset.sigs.k8s.io/worker-index": "1"}
    )
    unrelated = copy.deepcopy(leader)
    unrelated["metadata"]["ownerReferences"][0]["uid"] = "not-this-run"
    unrelated["metadata"]["ownerReferences"][0]["kind"] = "UnknownController"
    calls = []

    def get(resource, **kwargs):
        calls.append(resource)
        if resource == "pods":
            return {"items": [leader, worker, unrelated]}
        assert resource == waiter.PRIMARY_RESOURCES["LeaderWorkerSet"]
        return primary

    monkeypatch.setattr(waiter, "oc_get_json", get)
    monkeypatch.setattr(
        waiter, "oc", lambda *args, **kwargs: pytest.fail("endpoint discovery must not mutate")
    )
    ctx = SimpleNamespace()
    args = SimpleNamespace(
        namespace="target",
        kind="LeaderWorkerSet",
        name="authored",
        uid="root",
        port=8000,
        artifact_dir=tmp_path,
    )
    waiter.resolve_endpoint.original_func(args, ctx)
    assert ctx.endpoint_url == "http://10.0.0.5:8000"
    assert "services" not in calls  # A portless operator headless Service is immaterial.


def test_owner_chain_checks_controller_uids_not_labels_or_name_prefix(monkeypatch):
    pod = _owned(
        "Pod",
        "authored-looks-related",
        "pod",
        owner_uid="rs",
        owner_kind="ReplicaSet",
        owner_version="apps/v1",
    )
    replica = _owned(
        "ReplicaSet", "authored", "rs", owner_kind="Deployment", owner_version="apps/v1"
    )
    monkeypatch.setattr(waiter, "oc_get_json", lambda *args, **kwargs: replica)
    assert waiter.owned_by(pod, "root", "target")
    replica["metadata"]["uid"] = "replacement-rs"
    assert not waiter.owned_by(pod, "root", "target")
    pod["metadata"].pop("ownerReferences")
    pod["metadata"]["labels"] = {"app": "authored", "forge.openshift.io/run": "authored"}
    assert not waiter.owned_by(pod, "root", "target")


@pytest.mark.parametrize("kind", ["InferenceService", "LLMInferenceService"])
def test_controller_url_is_not_repaired_or_given_a_guessed_port(kind):
    primary = _manifest(kind)
    url = "https://controller.example/v1"
    primary["status"] = {"url": url} if kind == "InferenceService" else {"address": {"url": url}}
    assert waiter._status_endpoint(primary) == url


def _runtime_capture_objects(kind):
    primary = _manifest(kind)
    primary["metadata"].update(namespace="target", uid="root")
    if kind == "LeaderWorkerSet":
        templates = primary["spec"]["leaderWorkerTemplate"]
        specs = [templates[key]["spec"] for key in ("leaderTemplate", "workerTemplate")]
        names = ["vllm-leader", "vllm-worker"]
    elif kind == "LLMInferenceService":
        specs = [primary["spec"]["template"]]
        names = ["main"]
    else:
        specs = [primary["spec"]["template"]["spec"]]
        names = ["vllm"]
    pods = []
    for index, (spec, name) in enumerate(zip(specs, names, strict=True)):
        spec["containers"][0]["name"] = name
        pod = _owned(
            "Pod",
            f"owned-{index}",
            f"pod-{index}",
            owner_kind=kind,
            owner_version=primary["apiVersion"],
        )
        pod["spec"] = copy.deepcopy(spec)
        # An admitted-only field must remain separate from the authored primary
        # and must never be printed into run.log by runtime attribution capture.
        pod["spec"]["containers"][0]["env"].append(
            {"name": "ADMISSION_ONLY", "value": "do-not-log-env"}
        )
        pod["status"] = {
            "containerStatuses": [
                {
                    "containerID": f"cri-o://container-{index}",
                    "image": spec["containers"][0]["image"],
                    "imageID": "quay.io/example/server@sha256:" + "c" * 64,
                    "name": name,
                    "ready": True,
                }
            ]
        }
        pods.append(pod)
    return primary, pods


def _capture_environment(monkeypatch, tmp_path):
    test_dir = tmp_path / "01__test" / "001__inference_playbooks_test_dir"
    test_dir.mkdir(parents=True)
    if "ARTIFACT_DIR" in vars(test_phase.env):
        monkeypatch.delattr(test_phase.env, "ARTIFACT_DIR")
    monkeypatch.setattr(test_phase.env, "_GLOBAL_ARTIFACT_DIR", test_dir)
    monkeypatch.setattr(test_phase.env._tls_artifact_dir, "val", test_dir, raising=False)
    monkeypatch.setenv("ARTIFACT_DIR", str(test_dir))
    return test_dir


@pytest.mark.parametrize("kind", ["LeaderWorkerSet", "LLMInferenceService", "Deployment"])
def test_capture_preserves_consumer_locations_and_runtime_protocol(
    monkeypatch, tmp_path, caplog, kind
):
    test_dir = _capture_environment(monkeypatch, tmp_path)
    primary, pods = _runtime_capture_objects(kind)
    original = copy.deepcopy(primary)
    unrelated = copy.deepcopy(pods[0])
    unrelated["metadata"].update(name="authored-unrelated", uid="unrelated", ownerReferences=[])
    unrelated["status"]["containerStatuses"][0]["imageID"] = "quay.io/unrelated@sha256:" + "d" * 64

    def get(resource, **kwargs):
        if resource == "pods":
            return {"items": [*pods, unrelated]}
        if resource == "deployments.apps" and "name" not in kwargs:
            return {"items": []}
        assert resource == test_phase._resource_type(primary)
        return primary

    monkeypatch.setattr(test_phase, "oc_get_json", get)
    monkeypatch.setattr(waiter, "oc_get_json", get)
    calls = []
    monkeypatch.setattr(test_phase, "oc", lambda *args, **kwargs: calls.append((args, kwargs)))
    caplog.set_level(logging.INFO, logger=test_phase.__name__)
    run_log = test_dir.parent / "run.log"
    handler = logging.FileHandler(run_log)
    test_phase.logger.addHandler(handler)
    try:
        # Exercise the real capture callback, real NextArtifactDir and real UID
        # filtering; only Kubernetes responses/log commands are mocked.
        test_phase._capture_created_recipe("target", [primary])
    finally:
        test_phase.logger.removeHandler(handler)
        handler.close()
    if kind == "Deployment":
        directory = test_dir / "artifacts"
        log = run_log.read_text()
        # deployment_serving_values splits run.log on a plain containerID key,
        # then requires these observed ready image/name/imageID fields.
        blocks = re.split(r"(?m)^\s*containerID:", log)[1:]
        assert len(blocks) == 1
        assert re.search(r"(?m)^\s*image: quay.io/example/server:pinned$", blocks[0])
        assert (
            re.search(r"(?m)^\s*imageID: \S*@(sha256:[a-f0-9]{64})$", blocks[0])[1]
            == "sha256:" + "c" * 64
        )
        assert re.search(r"(?m)^\s*name: vllm$", blocks[0])
        assert re.search(r"(?m)^\s*ready: true$", blocks[0])
        assert "do-not-log-env" not in log and "env:" not in log and "Secret" not in log
        assert "d" * 64 not in log
    else:
        capture_name = "lws" if kind == "LeaderWorkerSet" else "llmisvc"
        captures = [
            path
            for path in test_dir.iterdir()
            if path.is_dir() and re.fullmatch(r"\d+__capture_" + capture_name + "_state", path.name)
        ]
        assert len(captures) == 1
        directory = captures[0] / "artifacts"
        assert not (test_dir / "artifacts").exists()
    stem = kind.lower()
    assert yaml.safe_load((directory / f"{stem}.yaml").read_bytes())["metadata"]["uid"] == "root"
    captured = yaml.safe_load((directory / f"{stem}.pods.yaml").read_bytes())
    assert captured["kind"] == "List"
    assert captured["items"] == pods
    assert all(
        status["ready"] is True and status["imageID"].endswith("sha256:" + "c" * 64)
        for pod in captured["items"]
        for status in pod["status"]["containerStatuses"]
    )
    assert primary == original  # Capturing admission observations never changes the source.
    assert test_phase.env.ARTIFACT_DIR == test_dir
    assert [args[1] for args, _kwargs in calls] == [pod["metadata"]["name"] for pod in pods]
    assert all(kwargs["log_stdout"] is False for _args, kwargs in calls)
    assert "do-not-log-env" not in caplog.text


@pytest.mark.parametrize("kind", ["LeaderWorkerSet", "LLMInferenceService", "Deployment"])
def test_runtime_capture_rejects_replacement_before_creating_capture(monkeypatch, tmp_path, kind):
    test_dir = _capture_environment(monkeypatch, tmp_path)
    primary, _pods = _runtime_capture_objects(kind)
    replacement = copy.deepcopy(primary)
    replacement["metadata"]["uid"] = "replacement"
    monkeypatch.setattr(test_phase, "oc_get_json", lambda *args, **kwargs: replacement)
    monkeypatch.setattr(
        waiter, "owned_pods", lambda *args: pytest.fail("replacement must not be captured")
    )
    with pytest.raises(RuntimeError, match="capture UID changed"):
        test_phase._capture_created_recipe("target", [primary])
    assert list(test_dir.iterdir()) == []


@pytest.mark.parametrize("namespace", [None, "", "different"])
def test_launch_requires_explicit_selected_matching_namespace(launch, namespace):
    primary = _manifest()
    primary["metadata"]["namespace"] = "target"
    launch.checkout.path.write_text(yaml.safe_dump(primary))
    launch.checkout.snapshot()
    launch.values["inference_playbooks.namespace"] = namespace
    with pytest.raises(ValueError, match="namespace"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert not launch.src.exists()
    assert not launch.actions


def test_forbidden_rdma_override_fails_before_artifacts_or_cluster_calls(launch):
    launch.values["inference_playbooks.rdma_resource"] = "different.example/rdma"
    with pytest.raises(ValueError, match="RDMA"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert not launch.src.exists()
    assert not launch.actions


def test_preexisting_lws_operator_service_is_not_adopted(launch):
    launch.checkout.path.write_text(yaml.safe_dump(_manifest("LeaderWorkerSet")))
    launch.checkout.snapshot()
    launch.live[("services", "authored")] = {"metadata": {"uid": "unrelated"}}
    with pytest.raises(RuntimeError, match="operator Service name conflicts"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert not any(isinstance(args, tuple) for args, _kwargs in launch.actions)


def test_missing_namespace_is_not_provisioned(launch, monkeypatch):
    def forbidden(*args, **kwargs):
        raise RuntimeError("namespace unavailable")

    monkeypatch.setattr(test_phase, "oc_get_json", forbidden)
    with pytest.raises(RuntimeError, match="namespace unavailable"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert not any(isinstance(args, tuple) for args, _kwargs in launch.actions)


def test_success_response_without_uid_never_triggers_guessed_cleanup(launch, monkeypatch):
    monkeypatch.setattr(
        test_phase,
        "oc",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(_manifest()), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="ownership proof"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert launch.live == {}


def test_uid_precondition_is_sent_even_if_replacement_races_after_read(monkeypatch):
    created = _manifest()
    created["metadata"].update(namespace="target", uid="owned")
    monkeypatch.setattr(test_phase, "oc_get_json", lambda *args, **kwargs: created)
    requests = []

    def raced(*args, **kwargs):
        requests.append(json.loads(kwargs["input_text"]))
        return SimpleNamespace(returncode=1, stdout="", stderr="Conflict: UID precondition failed")

    monkeypatch.setattr(test_phase, "oc", raced)
    with pytest.raises(RuntimeError, match="UID-preconditioned deletion failed"):
        test_phase._delete_created_resources("target", [created])
    assert requests[0]["preconditions"] == {"uid": "owned"}


@pytest.mark.parametrize(
    "url",
    [
        "http://user:do-not-print@example.test",
        "https://example.test?token=do-not-print",
        "file:///tmp/work",
        "http://example:bad-port",
        "http://[broken]",
    ],
)
def test_invalid_or_credentialed_status_endpoint_is_not_logged(url):
    primary = _manifest("InferenceService")
    primary["status"] = {"url": url}
    with pytest.raises(ValueError) as error:
        waiter._status_endpoint(primary)
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize("kind", ["Deployment", "LeaderWorkerSet"])
def test_ambiguous_pod_endpoint_fails_instead_of_selecting_first(monkeypatch, tmp_path, kind):
    primary = _manifest(kind)
    primary["metadata"].update(namespace="target", uid="root")
    pod = _owned("Pod", "first", "pod")
    pod["metadata"]["labels"] = {"leaderworkerset.sigs.k8s.io/worker-index": "0"}
    pod["status"] = {"podIP": "10.0.0.5", "conditions": [{"type": "Ready", "status": "True"}]}
    monkeypatch.setattr(waiter, "get_primary", lambda *args: primary)
    monkeypatch.setattr(waiter, "owned_pods", lambda *args: [pod, copy.deepcopy(pod)])
    args = SimpleNamespace(
        namespace="target", kind=kind, name="authored", uid="root", port=8000, artifact_dir=tmp_path
    )
    with pytest.raises(ValueError, match="Multiple owned endpoint pods"):
        waiter.resolve_endpoint.original_func(args, SimpleNamespace())


def test_portless_headless_service_is_not_an_http_endpoint():
    service = _owned("Service", "authored", "service")
    service["spec"] = {"clusterIP": "None"}
    assert waiter._service_endpoint(service, "target", 8000) is None


def test_mapping_order_is_not_a_workload_change():
    source = [_manifest()]
    submitted = contract.normalized_documents(source, "target")
    submitted[0] = dict(reversed(list(submitted[0].items())))
    contract.assert_submission(source, submitted, "target")


def test_cannot_narrow_multidocument_file_to_primary(checkout):
    checkout.path.write_text(
        yaml.safe_dump_all(
            [_manifest(), {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "authored"}}]
        )
    )
    checkout.snapshot()
    with pytest.raises(ValueError, match="Unsupported"):
        contract.load_source_bundle(checkout.root, checkout.recipe, checkout.path)


def test_valid_multidocument_file_records_complete_document_count(checkout):
    checkout.path.write_text(
        yaml.safe_dump_all([_manifest("InferenceService"), _manifest("ServingRuntime")])
    )
    checkout.snapshot()
    bundle = contract.load_source_bundle(checkout.root, checkout.recipe, checkout.path)
    assert len(bundle["documents"]) == 2
    assert bundle["source_files"][0]["document_count"] == 2


def test_authored_name_cannot_overwrite_fixed_bundle_artifacts(launch):
    source = _manifest("LLMInferenceService")
    source["metadata"]["name"] = "source-bundle"
    launch.checkout.path.write_text(yaml.safe_dump(source))
    launch.checkout.snapshot()
    test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    recorded_source = yaml.safe_load((launch.src / "source-bundle.yaml").read_bytes())
    assert recorded_source == source
    assert (launch.src / "launch-source-bundle.yaml").is_file()


@pytest.mark.parametrize(
    "value",
    [
        "__missing__",
        None,
        "",
        "  ",
        123,
        [],
        {},
        "not-an-artifact.yaml",
        "../outside.yaml",
        "./models/model/recipes/recipe/manifests/vllm-v1/b-server.yaml",
        "models/model/recipes/recipe/recipe.yaml",
    ],
)
def test_every_selected_launch_requires_explicit_canonical_manifest_path(launch, value):
    if value == "__missing__":
        launch.values.pop("inference_playbooks.manifest_path")
    else:
        launch.values["inference_playbooks.manifest_path"] = value
    with pytest.raises(ValueError, match="manifest_path"):
        test_phase._launch_recipe("synthetic", launch.checkout.recipe)
    assert not launch.actions
    assert not launch.src.exists()


@pytest.mark.parametrize("value", ["__missing__", None, "", "  ", 123])
def test_selected_recipe_without_explicit_path_fails_at_entrypoint(checkout, monkeypatch, value):
    values = {"inference_playbooks.recipe": "model/recipe"}
    if value != "__missing__":
        values["inference_playbooks.manifest_path"] = value
    monkeypatch.setattr(test_phase.foreign_repository, "initialize", lambda: checkout.root)
    monkeypatch.setattr(
        test_phase.config, "project", SimpleNamespace(get_config=lambda key: values.get(key))
    )
    monkeypatch.setattr(
        test_phase,
        "_load_recipe_metadata",
        lambda *args: pytest.fail("missing path must fail before source selection"),
    )
    monkeypatch.setattr(test_phase, "oc", lambda *args, **kwargs: pytest.fail("must not submit"))
    with pytest.raises(ValueError, match="requires an explicit nonempty"):
        test_phase.do_test()


def test_even_single_platform_bundle_cannot_be_inferred(checkout):
    with pytest.raises(ValueError, match="cannot be inferred"):
        contract.load_source_bundle(checkout.root, checkout.recipe)


def test_catalog_only_validation_does_not_require_path_or_launch(checkout, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(test_phase.foreign_repository, "initialize", lambda: checkout.root)
    monkeypatch.setattr(test_phase.config, "project", SimpleNamespace(get_config=lambda key: None))
    monkeypatch.setattr(
        test_phase, "load_recipe_catalog", lambda root: calls.append("catalog") or {}
    )
    monkeypatch.setattr(test_phase.env, "NextArtifactDir", lambda name: nullcontext())
    monkeypatch.setattr(test_phase, "create_custom_test_metadata", lambda recipe_id: tmp_path)
    monkeypatch.setattr(test_phase, "update_test_labels_with_timing", lambda *args: None)
    monkeypatch.setattr(test_phase, "update_test_labels_with_status", lambda *args: None)
    monkeypatch.setattr(
        test_phase, "_launch_recipe", lambda *args: pytest.fail("catalog-only must not launch")
    )
    assert test_phase.do_test() == 0
    assert calls == ["catalog"]


def test_waiter_entrypoint_runs_real_dsl_with_mocked_cluster(monkeypatch, tmp_path):
    import projects.core.dsl.runtime as runtime

    importlib.reload(waiter)  # Register tasks even when neighboring DSL tests reset the manager.
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    if "ARTIFACT_DIR" in vars(test_phase.env):
        monkeypatch.delattr(test_phase.env, "ARTIFACT_DIR")
    monkeypatch.setattr(test_phase.env, "_GLOBAL_ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(test_phase.env._tls_artifact_dir, "val", tmp_path, raising=False)
    # Do not persist the invoking developer's environment in offline test artifacts.
    monkeypatch.setattr(runtime, "_generate_env_file", lambda *args: None)
    primary = _manifest("InferenceService")
    primary["metadata"].update(namespace="target", uid="owned")
    primary["status"] = {"url": "http://controller.example/v1"}
    monkeypatch.setattr(waiter, "oc_get_json", lambda *args, **kwargs: primary)
    calls = []
    monkeypatch.setattr(waiter, "oc", lambda *args, **kwargs: calls.append(args))
    endpoint = waiter.run(
        namespace="target",
        kind="InferenceService",
        name="authored",
        uid="owned",
        timeout_seconds=90,
    )
    assert endpoint == "http://controller.example/v1"
    assert calls[0][0] == "wait"
    assert "--timeout=90s" in calls[0]
    assert (
        next(tmp_path.glob("*__wait_recipe/artifacts/endpoint.url")).read_text().strip() == endpoint
    )


def test_direct_selection_reads_only_selected_recipe_and_dependencies(checkout, monkeypatch):
    recipe_path = checkout.recipe["recipe_path"]
    with recipe_path.open("a") as handle:
        handle.write(
            "image:\n  recommended:\n    status: {date: 2026-09-28}\n  pin_for_disconnected:\n    status: {date: 2026-09-28}\n"
        )
    model_path = checkout.root / "models/model/model.yaml"
    hardware_path = checkout.root / "hardware.yaml"
    for path in (model_path, hardware_path):
        with path.open("a") as handle:
            handle.write("metadata_date: 2026-09-28\n")
    unrelated = checkout.root / "models/qwen/recipes/unrelated/recipe.yaml"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(
        "recipe_id: synthetic\nimage: {recommended: {status: {date: 2026-09-28}}}\n"
    )
    broken = unrelated.parent.parent / "broken/recipe.yaml"
    broken.parent.mkdir()
    broken.write_text(
        "recipe_id: same\nrecipe_id: duplicate\nunsupported: !!python/object:do_not_construct {}\n"
    )
    checkout.snapshot()
    allowed = {recipe_path, model_path, hardware_path, checkout.path}
    seen = set()
    original_open, original_glob = Path.open, Path.glob

    def guarded_open(path, mode="r", *args, **kwargs):
        assert path in allowed, f"Read outside selected recipe and dependencies: {path}"
        seen.add(path)
        return original_open(path, mode, *args, **kwargs)

    def guarded_glob(path, pattern, *args, **kwargs):
        assert path == checkout.folder and pattern == "*", "Repository-wide discovery is forbidden"
        return original_glob(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "glob", guarded_glob)
    monkeypatch.setattr(
        Path, "rglob", lambda *args, **kwargs: pytest.fail("recursive discovery is forbidden")
    )
    recipe = test_phase._load_recipe_metadata(checkout.root, "model/recipe")
    bundle = test_phase._load_manifest_override(
        checkout.root, "manifests/vllm-v1/b-server.yaml", recipe
    )
    assert seen == allowed
    assert recipe["recipe_id"] == bundle["recipe_id"] == "synthetic"
    assert recipe["image"]["recommended"]["status"]["date"] == "2026-09-28"
    assert recipe["image"]["pin_for_disconnected"]["status"]["date"] == "2026-09-28"
    assert recipe["hardware_profile_data"]["metadata_date"] == "2026-09-28"
    assert bundle["recipe_path"] == "models/model/recipes/recipe/recipe.yaml"
    assert (
        bundle["source_files"][0]["path"]
        == "models/model/recipes/recipe/manifests/vllm-v1/b-server.yaml"
    )


@pytest.mark.parametrize(
    "selector",
    [
        None,
        "",
        123,
        [],
        "synthetic",
        "model",
        "model/recipe/extra",
        "/model/recipe",
        "model/../recipe",
        "./model/recipe",
        "model//recipe",
        "model/recipe/",
        "models/model/recipes/recipe",
        "model/missing",
    ],
)
def test_invalid_selector_has_no_scan_or_opaque_id_fallback(checkout, monkeypatch, selector):
    monkeypatch.setattr(Path, "glob", lambda *args, **kwargs: pytest.fail("must not scan"))
    monkeypatch.setattr(
        Path, "open", lambda *args, **kwargs: pytest.fail("must not read fallback inputs")
    )
    with pytest.raises(ValueError):
        test_phase._load_recipe_metadata(checkout.root, selector)


@pytest.mark.parametrize("target", ["recipe", "model", "hardware"])
def test_selected_metadata_still_rejects_duplicate_keys(checkout, target):
    paths = {
        "recipe": checkout.recipe["recipe_path"],
        "model": checkout.root / "models/model/model.yaml",
        "hardware": checkout.root / "hardware.yaml",
    }
    with paths[target].open("a") as handle:
        handle.write("metadata_date: 2026-09-28\nmetadata_date: 2026-10-22\n")
    with pytest.raises(ValueError, match="Invalid metadata YAML"):
        test_phase._load_recipe_metadata(checkout.root, "model/recipe")


@pytest.mark.parametrize(
    "raw",
    [b"x: !!set {a: null}", b"x: !!python/object:unsupported {}", b"x: &cycle {again: *cycle}"],
)
def test_metadata_parser_does_not_hide_unsupported_yaml(raw):
    with pytest.raises(ValueError, match="Invalid metadata YAML"):
        contract.load_metadata_document(raw)


def test_metadata_dates_do_not_relax_workload_yaml():
    raw = b"date: 2026-09-28\ntimestamp: 2026-09-28T01:02:03Z\n"
    assert contract.load_metadata_document(raw) == {
        "date": "2026-09-28",
        "timestamp": "2026-09-28T01:02:03Z",
    }
    with pytest.raises(ValueError, match="Invalid manifest YAML"):
        contract.load_documents(raw)


@pytest.mark.parametrize("target", ["recipe-file", "recipe-folder", "model", "hardware"])
def test_selector_and_dependency_symlinks_are_rejected(checkout, tmp_path, target):
    recipe_path = checkout.recipe["recipe_path"]
    if target == "recipe-folder":
        alias = recipe_path.parent.parent / "alias"
        alias.symlink_to(recipe_path.parent, target_is_directory=True)
        selector = "model/alias"
    else:
        paths = {
            "recipe-file": recipe_path,
            "model": checkout.root / "models/model/model.yaml",
            "hardware": checkout.root / "hardware.yaml",
        }
        path = paths[target]
        outside = tmp_path / "outside.yaml"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        selector = "model/recipe"
    with pytest.raises(ValueError, match="without symlinks"):
        test_phase._load_recipe_metadata(checkout.root, selector)


@pytest.mark.parametrize(
    "selection",
    [
        "models/model/recipes/recipe/manifests/vllm-v1/b-server.yaml",
        "../manifests/vllm-v1/b-server.yaml",
        "./manifests/vllm-v1/b-server.yaml",
        "manifests/vllm-v1/../vllm-v1/b-server.yaml",
        "manifests/vllm-v1//b-server.yaml",
        "manifests/rhoai-3.5/server.yaml",
        "absolute",
        "symlink",
        "escape",
    ],
)
def test_recipe_relative_manifest_path_is_canonical_and_selected(checkout, tmp_path, selection):
    if selection == "absolute":
        selection = str(checkout.path)
    elif selection in {"symlink", "escape"}:
        target = checkout.path
        if selection == "escape":
            target = tmp_path / "outside.yaml"
            target.write_bytes(checkout.path.read_bytes())
        alias = checkout.folder / "alias.yaml"
        alias.symlink_to(target)
        selection = "manifests/vllm-v1/alias.yaml"
    elif "rhoai" in selection:
        wrong = checkout.recipe["recipe_path"].parent / selection
        wrong.parent.mkdir()
        wrong.write_bytes(checkout.path.read_bytes())
    with pytest.raises(ValueError):
        test_phase._load_manifest_override(checkout.root, selection, checkout.recipe)


def test_selected_labels_use_authored_id_without_rewriting_config(checkout, monkeypatch, tmp_path):
    values = {
        "inference_playbooks.recipe": "model/recipe",
        "inference_playbooks.manifest_path": "manifests/vllm-v1/b-server.yaml",
    }
    unchanged = copy.deepcopy(values)
    labels = []
    calls = []
    monkeypatch.setattr(test_phase.foreign_repository, "initialize", lambda: checkout.root)
    monkeypatch.setattr(
        test_phase.config,
        "project",
        SimpleNamespace(
            get_config=lambda key: values.get(key),
            set_config=lambda *args: pytest.fail("must not rewrite selectors"),
        ),
    )
    monkeypatch.setattr(test_phase.env, "NextArtifactDir", lambda name: nullcontext())
    monkeypatch.setattr(test_phase.env, "ARTIFACT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(test_phase, "_get_workload_configs", lambda: [("fixture", {})])
    monkeypatch.setattr(
        test_phase, "create_test_metadata", lambda directory, data: labels.append(data)
    )
    monkeypatch.setattr(test_phase, "update_test_labels_with_timing", lambda *args: None)
    monkeypatch.setattr(test_phase, "update_test_labels_with_status", lambda *args: None)

    def launch(recipe_id, recipe):
        bundle = test_phase._manifest_bundle_from_recipe(recipe)
        assert bundle["recipe_id"] == recipe_id == "synthetic"
        kpis = test_phase._benchmark_kpi_labels(recipe_id, recipe, "fixture")
        assert kpis["deployment_profile"] == "synthetic"
        calls.append(recipe_id)

    monkeypatch.setattr(test_phase, "_launch_recipe", launch)
    assert test_phase.do_test() == 0
    assert labels[0]["recipe_id"] == "synthetic"
    assert calls == ["synthetic"]
    assert values == unchanged
