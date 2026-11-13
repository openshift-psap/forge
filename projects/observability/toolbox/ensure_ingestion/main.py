"""Ensure one Forge run's Thanos Receiver and OpenShift monitoring queues.

This toolbox has no dependency on a host project's orchestration. Its caller
passes every cluster-specific value explicitly. Secret bytes stay in memory and
go to ``oc`` only through stdin with logging suppressed.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.template import render_template
from projects.core.dsl.utils.k8s import oc

_TEMPLATES = Path(__file__).parent / "templates"
_OWNER_LABEL = "forge.openshift.io/component"
_OWNER_VALUE = "observability"
_RUN_SUFFIX_LABEL = "forge.openshift.io/run-suffix"
_RUN_ID_ANNOTATION = "forge.openshift.io/run-id"
_TEARDOWN_PHASE_ANNOTATION = "forge.openshift.io/teardown-phase"
_SETUP_COMPLETE_ANNOTATION = "forge.openshift.io/setup-complete"
_CLUSTER_MONITORING_NAMESPACE = "openshift-monitoring"
_CLUSTER_CONFIGMAP = "cluster-monitoring-config"
_UWM_NAMESPACE = "openshift-user-workload-monitoring"
_UWM_CONFIGMAP = "user-workload-monitoring-config"
_QUEUE_CONFIG = {
    "capacity": 10000,
    "maxShards": 50,
    "minShards": 1,
    "maxSamplesPerSend": 5000,
    "batchSendDeadline": "5s",
    "minBackoff": "30ms",
    "maxBackoff": "5s",
}
_DEFAULT_RECEIVER_RESOURCES = {
    "requests": {"cpu": "1000m", "memory": "4Gi"},
    "limits": {"cpu": "2000m", "memory": "8Gi"},
}
_MONITORING_STACKS = (
    (_CLUSTER_MONITORING_NAMESPACE, "k8s"),
    (_UWM_NAMESPACE, "user-workload"),
)
_PROMETHEUS_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+([^\s]+)")
_PROMETHEUS_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
_QUEUE_METRICS = {
    "prometheus_remote_storage_queue_highest_timestamp_seconds",
    "prometheus_remote_storage_queue_highest_sent_timestamp_seconds",
    "prometheus_remote_storage_samples_pending",
    "prometheus_remote_storage_exemplars_pending",
    "prometheus_remote_storage_histograms_pending",
    "prometheus_remote_storage_samples_failed_total",
    "prometheus_remote_storage_exemplars_failed_total",
    "prometheus_remote_storage_histograms_failed_total",
}


@dataclass(frozen=True)
class QueueSnapshot:
    highest_enqueued: float
    highest_sent: float
    samples_pending: float
    exemplars_pending: float
    histograms_pending: float
    samples_failed: float
    exemplars_failed: float
    histograms_failed: float
    unexpected_drops: float

    def drained(self) -> bool:
        return (
            self.samples_pending == 0
            and self.exemplars_pending == 0
            and self.histograms_pending == 0
            and self.highest_sent >= self.highest_enqueued
            and self.samples_failed == 0
            and self.exemplars_failed == 0
            and self.histograms_failed == 0
            and self.unexpected_drops == 0
        )


@entrypoint
def run(
    *,
    run_id: str,
    run_suffix: str,
    receiver_namespace: str,
    receiver_name: str,
    receiver_image: str,
    receiver_storage_class: str | None,
    receiver_pvc_size: str,
    receiver_retention: str,
    receiver_out_of_order_window: str | None,
    receiver_run_as_user: int | None,
    receiver_fs_group: int | None,
    receiver_resources: dict[str, Any] | None,
    receiver_shutdown_grace_seconds: int,
    objstore_content_path: str,
    cluster_label: str,
    workload_namespace: str,
    queue_name: str,
    namespace_regex: str,
    container_names: list[str],
    container_metric_regex: str,
    forward_gpu_metrics: bool,
    gpu_namespaces: list[str],
    gpu_workload_label: str | None,
    gpu_metric_regex: str,
    service_monitors: list[dict[str, Any]],
) -> int:
    """Reconcile this run's Receiver, named queues, and ServiceMonitors."""

    execute_tasks(locals())
    return 0


def _resource_id(kind: str, name: str, namespace: str | None) -> str:
    return f"{namespace}/{kind}/{name}" if namespace else f"{kind}/{name}"


def _read_resource(
    kind: str,
    name: str,
    *,
    namespace: str | None = None,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    args = ["get", kind, name]
    if namespace:
        args.extend(["-n", namespace])
    args.extend(["-o", "json"])
    result = oc(*args, check=False, handled_secretly=True)
    resource_id = _resource_id(kind, name, namespace)
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if allow_missing and ("(notfound)" in stderr or "not found" in stderr):
            return None
        if "forbidden" in stderr or "unauthorized" in stderr:
            raise PermissionError(f"Cannot read {resource_id}; observability bootstrap needs RBAC")
        raise RuntimeError(f"Cannot read {resource_id}; oc get failed")
    try:
        value = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(f"Invalid JSON returned for {resource_id}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid object returned for {resource_id}")
    return value


def _apply_manifest(manifest: dict[str, Any], *, secret: bool = False) -> None:
    kind = manifest["kind"]
    metadata = manifest["metadata"]
    resource_id = _resource_id(kind, metadata["name"], metadata.get("namespace"))
    args = ["apply"]
    if secret:
        args.extend(["--server-side", "--field-manager=forge-observability"])
    args.extend(["-f", "-"])
    result = oc(
        *args,
        input_text=yaml.safe_dump(manifest, sort_keys=False),
        check=False,
        handled_secretly=True,
    )
    if result.returncode == 0:
        return
    if "forbidden" in result.stderr.lower() or "unauthorized" in result.stderr.lower():
        raise PermissionError(f"Cannot apply {resource_id}; observability bootstrap needs RBAC")
    raise RuntimeError(f"Cannot apply {resource_id}; oc apply failed")


def _owner_metadata(run_id: str, run_suffix: str) -> dict[str, dict[str, str]]:
    return {
        "labels": {
            _OWNER_LABEL: _OWNER_VALUE,
            _RUN_SUFFIX_LABEL: run_suffix,
        },
        "annotations": {_RUN_ID_ANNOTATION: run_id},
    }


def _require_managed(
    existing: dict[str, Any],
    kind: str,
    name: str,
    namespace: str,
    run_id: str,
    run_suffix: str,
) -> None:
    metadata = existing.get("metadata") or {}
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    if (
        labels.get(_OWNER_LABEL) != _OWNER_VALUE
        or labels.get(_RUN_SUFFIX_LABEL) != run_suffix
        or annotations.get(_RUN_ID_ANNOTATION) != run_id
    ):
        raise RuntimeError(f"{_resource_id(kind, name, namespace)} belongs to another owner or run")


def _ensure_namespace(namespace: str, run_id: str, run_suffix: str) -> None:
    existing = _read_resource("namespace", namespace, allow_missing=True)
    if existing is not None:
        _require_managed(existing, "Namespace", namespace, "", run_id, run_suffix)
        if existing.get("metadata", {}).get("deletionTimestamp"):
            raise RuntimeError(f"Namespace/{namespace} is being deleted")
        phase = (existing.get("metadata", {}).get("annotations") or {}).get(
            _TEARDOWN_PHASE_ANNOTATION
        )
        if phase:
            raise RuntimeError(
                f"Namespace/{namespace} has teardown phase {phase}; "
                "finish teardown before provisioning this run again"
            )
        return
    manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            **_owner_metadata(run_id, run_suffix),
        },
    }
    result = oc(
        "create",
        "-f",
        "-",
        input_text=yaml.safe_dump(manifest, sort_keys=False),
        check=False,
        handled_secretly=True,
    )
    if result.returncode == 0:
        return
    if "alreadyexists" in result.stderr.lower():
        _ensure_namespace(namespace, run_id, run_suffix)
        return
    if "forbidden" in result.stderr.lower() or "unauthorized" in result.stderr.lower():
        raise PermissionError(f"Cannot create namespace/{namespace}; RBAC is required")
    raise RuntimeError(f"Cannot create namespace/{namespace}; oc create failed")


def _update_namespace_annotations(
    namespace: str,
    run_id: str,
    run_suffix: str,
    mutator: Callable[[dict[str, str]], bool],
) -> None:
    """Change only owned namespace annotations with resourceVersion CAS."""

    for _ in range(5):
        current = _read_resource("namespace", namespace)
        _require_managed(current, "Namespace", namespace, "", run_id, run_suffix)
        manifest = copy.deepcopy(current)
        manifest.pop("status", None)
        metadata = manifest["metadata"]
        metadata.pop("managedFields", None)
        annotations = metadata.setdefault("annotations", {})
        if not mutator(annotations):
            return
        result = oc(
            "replace",
            "-f",
            "-",
            input_text=yaml.safe_dump(manifest, sort_keys=False),
            check=False,
            handled_secretly=True,
        )
        if result.returncode == 0:
            return
        stderr = result.stderr.lower()
        if "conflict" in stderr or "object has been modified" in stderr:
            continue
        if "forbidden" in stderr or "unauthorized" in stderr:
            raise PermissionError(f"Cannot update Namespace/{namespace}; RBAC is required")
        raise RuntimeError(f"Cannot update Namespace/{namespace}; oc replace failed")
    raise RuntimeError(f"Could not update Namespace/{namespace} after conflicts")


def _receiver_manifests(args: Any, objstore_digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rendered = render_template(
        "receiver.yaml.j2",
        {
            "name": args.receiver_name,
            "namespace": args.receiver_namespace,
            "image": args.receiver_image,
            "pvc_size": args.receiver_pvc_size,
            "storage_class": args.receiver_storage_class,
            "retention": args.receiver_retention,
            "out_of_order_window": args.receiver_out_of_order_window,
            "run_as_user": args.receiver_run_as_user,
            "fs_group": args.receiver_fs_group,
            "resources": args.receiver_resources or _DEFAULT_RECEIVER_RESOURCES,
            "cluster_label": args.cluster_label,
            "run_id": args.run_id,
            "run_suffix": args.run_suffix,
            "objstore_secret": f"{args.receiver_name}-objstore",
            "objstore_digest": objstore_digest,
            "shutdown_grace_seconds": args.receiver_shutdown_grace_seconds,
        },
        templates_dir=_TEMPLATES,
    )
    documents = list(yaml.safe_load_all(rendered))
    if len(documents) != 2 or [item.get("kind") for item in documents] != [
        "Service",
        "StatefulSet",
    ]:
        raise RuntimeError("Receiver template did not produce Service and StatefulSet")
    return documents[0], documents[1]


def _validate_immutable_statefulset(
    existing: dict[str, Any], desired: dict[str, Any], namespace: str, name: str
) -> None:
    old_spec = existing.get("spec") or {}
    new_spec = desired["spec"]
    for field in ("serviceName", "selector"):
        if old_spec.get(field) != new_spec.get(field):
            raise RuntimeError(
                f"{namespace}/StatefulSet/{name} has incompatible immutable {field}; "
                "choose a new Receiver namespace"
            )

    old_templates = old_spec.get("volumeClaimTemplates") or []
    new_templates = new_spec["volumeClaimTemplates"]
    if len(old_templates) != 1 or old_templates[0].get("metadata", {}).get("name") != "data":
        raise RuntimeError(f"{namespace}/StatefulSet/{name} has incompatible PVC templates")
    old_labels = old_templates[0].get("metadata", {}).get("labels") or {}
    new_labels = new_templates[0].get("metadata", {}).get("labels") or {}
    if any(old_labels.get(key) != value for key, value in new_labels.items()):
        raise RuntimeError(f"{namespace}/StatefulSet/{name} has incompatible PVC labels")
    old_pvc = old_templates[0].get("spec") or {}
    new_pvc = new_templates[0]["spec"]
    if old_pvc.get("accessModes") != new_pvc.get("accessModes"):
        raise RuntimeError(f"{namespace}/StatefulSet/{name} has incompatible PVC access modes")
    if (old_pvc.get("resources") or {}).get("requests", {}).get("storage") != new_pvc["resources"][
        "requests"
    ]["storage"]:
        raise RuntimeError(
            f"{namespace}/StatefulSet/{name} has a different PVC size; "
            "StatefulSet volumeClaimTemplates cannot be changed in place"
        )
    configured_class = new_pvc.get("storageClassName")
    if configured_class is not None and old_pvc.get("storageClassName") != configured_class:
        raise RuntimeError(f"{namespace}/StatefulSet/{name} has a different PVC StorageClass")


def _validate_existing_service(service: dict[str, Any], name: str, namespace: str) -> None:
    spec = service.get("spec") or {}
    if spec.get("type", "ClusterIP") != "ClusterIP":
        raise RuntimeError(f"{namespace}/Service/{name} must be a ClusterIP Service")
    ports = spec.get("ports") or []
    available_ports = {port.get("port") for port in ports}
    if not {19291, 10901, 10902}.issubset(available_ports):
        raise RuntimeError(
            f"{namespace}/Service/{name} must expose remote-write, gRPC, and HTTP ports"
        )


def _receiver_arguments(statefulset: dict[str, Any], name: str, namespace: str) -> list[str]:
    containers = (
        statefulset.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    )
    receiver_containers = [item for item in containers if "receive" in (item.get("args") or [])]
    if len(receiver_containers) != 1:
        raise RuntimeError(f"{namespace}/StatefulSet/{name} has no single Thanos receive container")
    return receiver_containers[0].get("args") or []


def _receiver_cluster_label(receiver_args: list[str]) -> str | None:
    labels = [
        item.removeprefix("--label=cluster=").strip('"')
        for item in receiver_args
        if item.startswith("--label=cluster=")
    ]
    return labels[0] if len(labels) == 1 else None


def _receiver_run_label(receiver_args: list[str]) -> str | None:
    labels = [
        item.removeprefix("--label=forge_run_id=").strip('"')
        for item in receiver_args
        if item.startswith("--label=forge_run_id=")
    ]
    return labels[0] if len(labels) == 1 else None


def _objstore_identity(raw_config: bytes, source: str) -> tuple[Any, ...]:
    if not raw_config.strip():
        raise ValueError(f"{source} is empty")
    try:
        config = yaml.safe_load(raw_config)
    except (yaml.YAMLError, UnicodeError):
        # YAML diagnostics may quote credential values, so suppress their text.
        raise ValueError(f"{source} is invalid YAML") from None
    if not isinstance(config, dict) or config.get("type") != "S3":
        raise ValueError(f"{source} must have type: S3")
    s3_config = config.get("config")
    if (
        not isinstance(s3_config, dict)
        or not isinstance(s3_config.get("bucket"), str)
        or not s3_config["bucket"].strip()
    ):
        raise ValueError(f"{source} needs config.bucket")
    return (
        config["type"],
        s3_config["bucket"],
        s3_config.get("endpoint"),
        s3_config.get("region"),
        config.get("prefix"),
    )


def _existing_secret_identity(secret: dict[str, Any], namespace: str, name: str) -> tuple[Any, ...]:
    encoded = (secret.get("data") or {}).get("objstore.yaml")
    if not isinstance(encoded, str):
        raise ValueError(f"{namespace}/Secret/{name} is missing objstore.yaml")
    try:
        raw_config = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{namespace}/Secret/{name} has invalid objstore.yaml encoding") from None
    return _objstore_identity(raw_config, f"{namespace}/Secret/{name} object-store config")


def _ensure_receiver(args: Any) -> None:
    try:
        objstore_bytes = Path(args.objstore_content_path).read_bytes()
    except OSError:
        raise FileNotFoundError("Cannot read required observability vault content") from None
    objstore_identity = _objstore_identity(
        objstore_bytes, "Observability object-store vault content"
    )

    encoded = base64.b64encode(objstore_bytes).decode("ascii")
    digest = hashlib.sha256(objstore_bytes).hexdigest()
    service, statefulset = _receiver_manifests(args, digest)
    _ensure_namespace(args.receiver_namespace, args.run_id, args.run_suffix)
    namespace = args.receiver_namespace
    name = args.receiver_name
    secret_name = f"{name}-objstore"
    existing_statefulset = _read_resource(
        "statefulset", name, namespace=namespace, allow_missing=True
    )
    existing_service = _read_resource("service", name, namespace=namespace, allow_missing=True)
    existing_secret = _read_resource("secret", secret_name, namespace=namespace, allow_missing=True)
    if existing_statefulset is not None:
        _require_managed(
            existing_statefulset,
            "StatefulSet",
            name,
            namespace,
            args.run_id,
            args.run_suffix,
        )
        _validate_immutable_statefulset(existing_statefulset, statefulset, namespace, name)
        current_args = _receiver_arguments(existing_statefulset, name, namespace)
        if _receiver_cluster_label(current_args) != args.cluster_label:
            raise RuntimeError(
                f"{namespace}/StatefulSet/{name} has a different cluster label; "
                "changing historical Receiver identity requires a new run ID"
            )
        if _receiver_run_label(current_args) != args.run_id:
            raise RuntimeError(
                f"{namespace}/StatefulSet/{name} has a different forge_run_id block label"
            )
    if existing_service is not None:
        _require_managed(existing_service, "Service", name, namespace, args.run_id, args.run_suffix)
        if existing_service.get("spec", {}).get("clusterIP") == "None":
            raise RuntimeError(
                f"{namespace}/Service/{name} is headless and cannot become ClusterIP"
            )
        _validate_existing_service(existing_service, name, namespace)
    if existing_secret is not None:
        _require_managed(
            existing_secret,
            "Secret",
            secret_name,
            namespace,
            args.run_id,
            args.run_suffix,
        )
        if existing_secret.get("type") not in (None, "Opaque"):
            raise RuntimeError(f"{namespace}/Secret/{secret_name} must have type Opaque")
        existing_encoded = (existing_secret.get("data") or {}).get("objstore.yaml")
        if (
            existing_encoded != encoded
            and _existing_secret_identity(existing_secret, namespace, secret_name)
            != objstore_identity
        ):
            raise RuntimeError(
                f"{namespace}/Secret/{secret_name} has a different S3 destination; "
                "moving future blocks requires a new Receiver name"
            )
        if existing_secret.get("immutable") and existing_encoded != encoded:
            raise RuntimeError(f"{namespace}/Secret/{secret_name} is immutable and differs")

    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            **_owner_metadata(args.run_id, args.run_suffix),
        },
        "type": "Opaque",
        "data": {"objstore.yaml": encoded},
    }
    if (
        existing_secret is None
        or (existing_secret.get("data") or {}).get("objstore.yaml") != encoded
    ):
        _apply_manifest(secret_manifest, secret=True)
    _apply_manifest(service)
    _apply_manifest(statefulset)


@task
def ensure_receiver(args, ctx):
    """Create or reconcile this run's Forge-owned Receiver."""

    _ensure_receiver(args)
    return f"Thanos Receiver {args.receiver_namespace}/{args.receiver_name} configured"


@retry(attempts=60, delay=5, backoff=1.0)
@task
def wait_for_receiver(args, ctx):
    """Wait for Receiver rollout and a routable remote-write Service endpoint."""

    statefulset = _read_resource(
        "statefulset", args.receiver_name, namespace=args.receiver_namespace
    )
    desired_replicas = statefulset.get("spec", {}).get("replicas", 1)
    status = statefulset.get("status") or {}
    if not isinstance(desired_replicas, int) or desired_replicas < 1:
        raise RuntimeError(
            f"{args.receiver_namespace}/StatefulSet/{args.receiver_name} is scaled to zero"
        )
    if (status.get("readyReplicas") or 0) < desired_replicas:
        return False
    if (status.get("observedGeneration") or 0) < (
        statefulset.get("metadata", {}).get("generation") or 0
    ):
        return False
    if (status.get("updatedReplicas") or 0) < desired_replicas:
        return False
    current_revision = status.get("currentRevision")
    update_revision = status.get("updateRevision")
    if current_revision and update_revision and current_revision != update_revision:
        return False
    endpoints = _read_resource(
        "endpoints", args.receiver_name, namespace=args.receiver_namespace, allow_missing=True
    )
    if endpoints is None:
        return False
    for subset in endpoints.get("subsets") or []:
        if subset.get("addresses") and any(
            port.get("port") == 19291 for port in subset.get("ports") or []
        ):
            return f"Receiver {args.receiver_namespace}/{args.receiver_name} is ready"
    return False


def _remote_write_url(name: str, namespace: str) -> str:
    return f"http://{name}.{namespace}.svc.cluster.local:19291/api/v1/receive"


def _prometheus_pods(namespace: str, prometheus_name: str) -> list[str] | None:
    """Return every expected ready replica, or None while an Operator rolls out."""

    statefulset_name = f"prometheus-{prometheus_name}"
    statefulset = _read_resource(
        "statefulset", statefulset_name, namespace=namespace, allow_missing=True
    )
    if statefulset is None:
        return None
    replicas = (statefulset.get("spec") or {}).get("replicas", 1)
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
        raise RuntimeError(f"{namespace}/StatefulSet/{statefulset_name} has no replicas")
    status = statefulset.get("status") or {}
    if (status.get("readyReplicas") or 0) < replicas:
        return None
    if (status.get("observedGeneration") or 0) < (
        statefulset.get("metadata", {}).get("generation") or 0
    ):
        return None
    pods: list[str] = []
    for ordinal in range(replicas):
        name = f"{statefulset_name}-{ordinal}"
        pod = _read_resource("pod", name, namespace=namespace, allow_missing=True)
        if pod is None or (pod.get("metadata") or {}).get("deletionTimestamp"):
            return None
        conditions = (pod.get("status") or {}).get("conditions") or []
        if not any(
            item.get("type") == "Ready" and item.get("status") == "True" for item in conditions
        ):
            return None
        pods.append(name)
    return pods


def _parse_queue_metrics(metrics_text: str, queue_name: str, url: str) -> QueueSnapshot | None:
    values: dict[str, float] = {}
    unexpected_drops = 0.0
    for line in metrics_text.splitlines():
        if not line.startswith("prometheus_remote_storage_"):
            continue
        match = _PROMETHEUS_SAMPLE.match(line)
        if match is None:
            continue
        metric, labels_text, raw_value = match.groups()
        labels: dict[str, str] = {}
        for key, escaped_value in _PROMETHEUS_LABEL.findall(labels_text):
            try:
                labels[key] = json.loads('"' + escaped_value + '"')
            except json.JSONDecodeError:
                raise RuntimeError(
                    "Prometheus returned malformed remote-write metric labels"
                ) from None
        if labels.get("remote_name") != queue_name or labels.get("url") != url:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            raise RuntimeError("Prometheus returned malformed remote-write metric values") from None
        if not math.isfinite(value):
            raise RuntimeError("Prometheus returned non-finite remote-write metric values")
        if metric.endswith("_dropped_total"):
            if labels.get("reason") != "dropped_series":
                unexpected_drops += value
            continue
        if metric not in _QUEUE_METRICS:
            continue
        if metric in values:
            raise RuntimeError("Prometheus returned duplicate named queue metrics")
        values[metric] = value

    if not values and unexpected_drops == 0:
        return None
    prefix = "prometheus_remote_storage_"
    required = (
        "queue_highest_timestamp_seconds",
        "queue_highest_sent_timestamp_seconds",
        "samples_pending",
        "samples_failed_total",
    )
    if any(prefix + item not in values for item in required):
        raise RuntimeError("Prometheus named remote-write queue is missing required drain metrics")
    return QueueSnapshot(
        highest_enqueued=values[prefix + "queue_highest_timestamp_seconds"],
        highest_sent=values[prefix + "queue_highest_sent_timestamp_seconds"],
        samples_pending=values[prefix + "samples_pending"],
        exemplars_pending=values.get(prefix + "exemplars_pending", 0),
        histograms_pending=values.get(prefix + "histograms_pending", 0),
        samples_failed=values[prefix + "samples_failed_total"],
        exemplars_failed=values.get(prefix + "exemplars_failed_total", 0),
        histograms_failed=values.get(prefix + "histograms_failed_total", 0),
        unexpected_drops=unexpected_drops,
    )


def _stack_queue_snapshots(
    namespace: str, prometheus_name: str, queue_name: str, url: str
) -> dict[tuple[str, str], QueueSnapshot | None] | None:
    """Read a named queue from every replica of one Prometheus stack."""

    pods = _prometheus_pods(namespace, prometheus_name)
    if pods is None:
        return None
    snapshots: dict[tuple[str, str], QueueSnapshot | None] = {}
    for pod_name in pods:
        result = oc(
            "exec",
            "-n",
            namespace,
            pod_name,
            "-c",
            "prometheus",
            "--",
            "wget",
            "-qO-",
            "http://127.0.0.1:9090/metrics",
            check=False,
            timeout_seconds=30,
            handled_secretly=True,
        )
        if result.returncode != 0:
            return None
        snapshots[(namespace, pod_name)] = _parse_queue_metrics(result.stdout, queue_name, url)
    return snapshots


def _live_queue_snapshots(
    queue_name: str, url: str
) -> dict[tuple[str, str], QueueSnapshot | None] | None:
    """Read both stacks; full /metrics text stays in memory only."""

    snapshots: dict[tuple[str, str], QueueSnapshot | None] = {}
    for namespace, prometheus_name in _MONITORING_STACKS:
        stack = _stack_queue_snapshots(namespace, prometheus_name, queue_name, url)
        if stack is None:
            return None
        snapshots.update(stack)
    return snapshots


def _embedded_regex(pattern: str) -> str:
    """Remove outer anchors when inserting a regex between relabel separators."""

    if pattern.startswith("^"):
        pattern = pattern[1:]
    if pattern.endswith("$"):
        pattern = pattern[:-1]
    return f"(?:{pattern})"


def _cluster_remote_write_entry(args: Any) -> dict[str, Any]:
    namespace_pattern = _embedded_regex(args.namespace_regex)
    container_pattern = "(?:" + "|".join(args.container_names) + ")"
    alternatives = [
        f"{_embedded_regex(args.container_metric_regex)};{namespace_pattern};{container_pattern};.*",
        f"kube_pod_labels;{namespace_pattern};.*;.*",
    ]
    if args.forward_gpu_metrics:
        gpu_namespaces = "(?:" + "|".join(args.gpu_namespaces) + ")"
        alternatives.append(
            f"{_embedded_regex(args.gpu_metric_regex)};{gpu_namespaces};.*;{namespace_pattern}"
        )
    gpu_scope_label = args.gpu_workload_label or "exported_namespace"
    return {
        "name": args.queue_name,
        "url": _remote_write_url(args.receiver_name, args.receiver_namespace),
        "writeRelabelConfigs": [
            {
                "sourceLabels": ["__name__", "namespace", "container", gpu_scope_label],
                "regex": "|".join(f"(?:{alternative})" for alternative in alternatives),
                "action": "keep",
            },
            {"targetLabel": "cluster", "replacement": args.cluster_label},
        ],
        "queueConfig": copy.deepcopy(_QUEUE_CONFIG),
    }


def _uwm_remote_write_entry(args: Any) -> dict[str, Any]:
    return {
        "name": args.queue_name,
        "url": _remote_write_url(args.receiver_name, args.receiver_namespace),
        "writeRelabelConfigs": [
            {
                "sourceLabels": ["namespace"],
                "regex": args.namespace_regex,
                "action": "keep",
            },
            {"targetLabel": "cluster", "replacement": args.cluster_label},
        ],
        "queueConfig": copy.deepcopy(_QUEUE_CONFIG),
    }


def _merge_named_queue(config: dict[str, Any], section: str, entry: dict[str, Any]) -> bool:
    prometheus = config.setdefault(section, {})
    if not isinstance(prometheus, dict):
        raise ValueError(f"OpenShift monitoring config {section} must be a mapping")
    queues = prometheus.setdefault("remoteWrite", [])
    if not isinstance(queues, list):
        raise ValueError(f"OpenShift monitoring config {section}.remoteWrite must be a list")
    for existing in queues:
        if not isinstance(existing, dict):
            raise ValueError(
                f"OpenShift monitoring config {section}.remoteWrite has an invalid entry"
            )
        if existing.get("name") != entry["name"]:
            continue
        if existing == entry:
            return False
        raise RuntimeError(
            f"Named remoteWrite queue {entry['name']} already exists with different filters; "
            "choose a distinct monitoring.queue_name or one stable monitoring.namespace_regex "
            "that covers all concurrent Forge runs"
        )
    queues.append(entry)
    return True


def _update_configmap(
    namespace: str,
    name: str,
    mutator: Callable[[dict[str, Any]], bool],
) -> None:
    resource_id = _resource_id("ConfigMap", name, namespace)
    for _ in range(5):
        current = _read_resource("configmap", name, namespace=namespace, allow_missing=True)
        if current is None:
            current_config: dict[str, Any] = {}
            existing_data: dict[str, Any] = {}
        else:
            if current.get("immutable"):
                raise RuntimeError(f"{resource_id} is immutable and cannot be updated")
            existing_data = current.get("data") or {}
            raw_config = existing_data.get("config.yaml", "")
            try:
                parsed = yaml.safe_load(raw_config) if raw_config else {}
            except yaml.YAMLError:
                # A YAML parser exception can quote secret-bearing ConfigMap text.
                raise ValueError(f"{resource_id} contains invalid config.yaml") from None
            if not isinstance(parsed, dict):
                raise ValueError(f"{resource_id} config.yaml must contain a mapping")
            current_config = parsed

        changed = mutator(current_config)
        if not changed:
            return
        if current is None:
            manifest = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": namespace},
                "data": {"config.yaml": yaml.safe_dump(current_config, sort_keys=False)},
            }
            operation = "create"
        else:
            manifest = copy.deepcopy(current)
            manifest.pop("status", None)
            manifest["metadata"].pop("managedFields", None)
            manifest["data"] = copy.deepcopy(existing_data)
            manifest["data"]["config.yaml"] = yaml.safe_dump(current_config, sort_keys=False)
            operation = "replace"

        result = oc(
            operation,
            "-f",
            "-",
            input_text=yaml.safe_dump(manifest, sort_keys=False),
            check=False,
            handled_secretly=True,
        )
        if result.returncode == 0:
            return
        stderr = result.stderr.lower()
        if (
            "conflict" in stderr
            or "alreadyexists" in stderr
            or "object has been modified" in stderr
        ):
            continue
        if "forbidden" in stderr or "unauthorized" in stderr:
            raise PermissionError(
                f"Cannot update {resource_id}; observability bootstrap needs ConfigMap RBAC"
            )
        raise RuntimeError(f"Cannot update {resource_id}; oc {operation} failed")
    raise RuntimeError(f"Could not update {resource_id} after repeated resourceVersion conflicts")


@task
def configure_cluster_monitoring(args, ctx):
    """Enable UWM and add a Forge-only system metrics remote-write queue."""

    entry = _cluster_remote_write_entry(args)

    def merge(config: dict[str, Any]) -> bool:
        changed = config.get("enableUserWorkload") is not True
        config["enableUserWorkload"] = True
        return _merge_named_queue(config, "prometheusK8s", entry) or changed

    _update_configmap(_CLUSTER_MONITORING_NAMESPACE, _CLUSTER_CONFIGMAP, merge)
    return "OpenShift cluster monitoring remoteWrite configured"


@retry(attempts=60, delay=5, backoff=1.0)
@task
def wait_for_user_workload_monitoring(args, ctx):
    """Wait for the OpenShift user-workload Prometheus operator to be available."""
    namespace = _read_resource("namespace", _UWM_NAMESPACE, allow_missing=True)
    if namespace is None:
        return False
    operator = _read_resource(
        "deployment", "prometheus-operator", namespace=_UWM_NAMESPACE, allow_missing=True
    )
    if operator is None:
        return False
    if (operator.get("status") or {}).get("availableReplicas", 0) < 1:
        return False
    return "OpenShift user-workload monitoring operator is available"


@task
def configure_user_workload_monitoring(args, ctx):
    """Add a Forge namespace remote-write queue without changing other UWM settings."""

    entry = _uwm_remote_write_entry(args)

    def merge(config: dict[str, Any]) -> bool:
        return _merge_named_queue(config, "prometheus", entry)

    _update_configmap(_UWM_NAMESPACE, _UWM_CONFIGMAP, merge)
    return "OpenShift user-workload monitoring remoteWrite configured"


@retry(attempts=60, delay=5, backoff=1.0)
@task
def wait_for_remote_write_acceptance(args, ctx):
    """Verify both OpenShift Prometheus CRs accepted the named Receiver queue."""
    expected_url = _remote_write_url(args.receiver_name, args.receiver_namespace)
    for namespace, name in _MONITORING_STACKS:
        prometheus = _read_resource("prometheus", name, namespace=namespace, allow_missing=True)
        if prometheus is None:
            return False
        queues = prometheus.get("spec", {}).get("remoteWrite") or []
        matching = [entry for entry in queues if entry.get("name") == args.queue_name]
        if not matching:
            return False
        if len(matching) != 1 or matching[0].get("url") != expected_url:
            raise RuntimeError(
                f"{namespace}/Prometheus/{name} has a conflicting remoteWrite queue "
                f"named {args.queue_name}"
            )
    return f"Both Prometheus CRs accepted remoteWrite queue {args.queue_name}"


@retry(attempts=60, delay=5, backoff=1.0)
@task
def wait_for_live_remote_write_queues(args, ctx):
    """Wait until every Prometheus replica has registered this run's queue."""

    snapshots = _live_queue_snapshots(
        args.queue_name, _remote_write_url(args.receiver_name, args.receiver_namespace)
    )
    if snapshots is None or not snapshots or any(value is None for value in snapshots.values()):
        return False
    return f"Named remoteWrite queue {args.queue_name} is live on every Prometheus replica"


@task
def ensure_service_monitors(args, ctx):
    """Create configured ServiceMonitors in the workload namespace."""

    for monitor in args.service_monitors:
        existing = _read_resource(
            "servicemonitor",
            monitor["name"],
            namespace=args.workload_namespace,
            allow_missing=True,
        )
        if existing is not None:
            _require_managed(
                existing,
                "ServiceMonitor",
                monitor["name"],
                args.workload_namespace,
                args.run_id,
                args.run_suffix,
            )
        rendered = render_template(
            "servicemonitor.yaml.j2",
            {
                "monitor": monitor,
                "namespace": args.workload_namespace,
                "run_id": args.run_id,
                "run_suffix": args.run_suffix,
            },
            templates_dir=_TEMPLATES,
        )
        manifest = yaml.safe_load(rendered)
        if manifest.get("kind") != "ServiceMonitor":
            raise RuntimeError("ServiceMonitor template rendered an invalid resource")
        _apply_manifest(manifest)
    return f"Configured {len(args.service_monitors)} ServiceMonitor(s)"


@task
def mark_setup_complete(args, ctx):
    """Record that both queues and ServiceMonitors were live before benchmarks."""

    def mutate(annotations: dict[str, str]) -> bool:
        if annotations.get(_TEARDOWN_PHASE_ANNOTATION):
            raise RuntimeError(
                f"Namespace/{args.receiver_namespace} teardown has begun; "
                "provisioning cannot resume"
            )
        if annotations.get(_SETUP_COMPLETE_ANNOTATION) == "true":
            return False
        annotations[_SETUP_COMPLETE_ANNOTATION] = "true"
        return True

    _update_namespace_annotations(args.receiver_namespace, args.run_id, args.run_suffix, mutate)
    return f"Observability setup complete for run {args.run_suffix}"


if __name__ == "__main__":
    run.main()
