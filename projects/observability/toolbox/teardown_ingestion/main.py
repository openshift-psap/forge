"""Guarded teardown of one run-scoped Thanos Receiver.

The PVC is deleted only after all live Prometheus queues have drained,
the queue has disappeared from every replica, and the same Receiver
container has logged a successful final TSDB flush and S3 sync.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import yaml

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils.k8s import oc
from projects.observability.toolbox.ensure_ingestion.main import (
    _CLUSTER_CONFIGMAP,
    _CLUSTER_MONITORING_NAMESPACE,
    _SETUP_COMPLETE_ANNOTATION,
    _TEARDOWN_PHASE_ANNOTATION,
    _UWM_CONFIGMAP,
    _UWM_NAMESPACE,
    _read_resource,
    _remote_write_url,
    _require_managed,
    _stack_queue_snapshots,
    _update_configmap,
    _update_namespace_annotations,
)

_RETRY_DELAY_SECONDS = 5
_MAX_RETRY_ATTEMPTS = 999999
_PHASES = {"drained": 1, "queues-removed": 2, "uploaded": 3}
_SHUTDOWN_CHECKPOINT_ANNOTATION = "forge.openshift.io/shutdown-checkpoint"
_QUEUE_SENT_ANNOTATION = "forge.openshift.io/queue-sent-samples"
_STACK_CONFIG = (
    (_CLUSTER_MONITORING_NAMESPACE, _CLUSTER_CONFIGMAP, "prometheusK8s", "k8s"),
    (_UWM_NAMESPACE, _UWM_CONFIGMAP, "prometheus", "user-workload"),
)
_OPTIONAL_KINDS = {"inferenceservice", "llminferenceservice", "deploymentconfig"}


@entrypoint
def run(
    *,
    run_id: str,
    run_suffix: str,
    workload_namespace: str,
    receiver_namespace: str,
    receiver_name: str,
    queue_name: str,
    service_monitors: list[dict[str, Any]],
    stable_polls: int,
    workload_stop_timeout_seconds: int,
    queue_drain_timeout_seconds: int,
    queue_removal_timeout_seconds: int,
    receiver_shutdown_timeout_seconds: int,
) -> int:
    """Drain, upload, and delete only this run's owned ingestion resources."""

    execute_tasks(locals())
    return 0


def _list_resources(kind: str, namespace: str) -> list[dict[str, Any]]:
    result = oc(
        "get",
        kind,
        "-n",
        namespace,
        "-o",
        "json",
        check=False,
        handled_secretly=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if kind in _OPTIONAL_KINDS and (
            "the server doesn't have a resource type" in stderr
            or "no matches for kind" in stderr
            or "not recognized" in stderr
        ):
            return []
        if "(notfound)" in stderr or "not found" in stderr:
            if _read_resource("namespace", namespace, allow_missing=True) is None:
                return []
        if "forbidden" in stderr or "unauthorized" in stderr:
            raise PermissionError(
                f"Cannot list {namespace}/{kind}; observability teardown needs RBAC"
            )
        raise RuntimeError(f"Cannot list {namespace}/{kind}; oc get failed")
    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(f"Invalid JSON returned for {namespace}/{kind}") from None
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise RuntimeError(f"Invalid list returned for {namespace}/{kind}")
    return items


def _job_finished(item: dict[str, Any]) -> bool:
    if (item.get("spec") or {}).get("suspend") is True:
        return True
    conditions = (item.get("status") or {}).get("conditions") or []
    return any(
        condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True"
        for condition in conditions
    )


def _active_producers(namespace: str) -> dict[str, int]:
    """Count objects that can still send or recreate workload metrics."""

    if _read_resource("namespace", namespace, allow_missing=True) is None:
        return {}

    active: dict[str, int] = {}
    for kind in (
        "pod",
        "job",
        "deployment",
        "statefulset",
        "replicaset",
        "replicationcontroller",
        "daemonset",
        "cronjob",
        "deploymentconfig",
        "inferenceservice",
        "llminferenceservice",
    ):
        objects = _list_resources(kind, namespace)
        if kind == "pod":
            count = sum(
                (item.get("status") or {}).get("phase") not in {"Succeeded", "Failed"}
                for item in objects
            )
        elif kind == "job":
            count = sum(not _job_finished(item) for item in objects)
        elif kind in {
            "deployment",
            "statefulset",
            "replicaset",
            "replicationcontroller",
            "deploymentconfig",
        }:
            count = sum((item.get("spec") or {}).get("replicas", 1) != 0 for item in objects)
        elif kind == "cronjob":
            count = sum((item.get("spec") or {}).get("suspend") is not True for item in objects)
        else:
            # DaemonSets and KServe/llm-d service CRs can create Pods later.
            count = len(objects)
        if count:
            active[kind] = count
    return active


def _delete_service_monitors(args: Any) -> None:
    if _read_resource("namespace", args.workload_namespace, allow_missing=True) is None:
        return
    for monitor in args.service_monitors:
        name = monitor["name"]
        current = _read_resource(
            "servicemonitor",
            name,
            namespace=args.workload_namespace,
            allow_missing=True,
        )
        if current is None:
            continue
        _require_managed(
            current,
            "ServiceMonitor",
            name,
            args.workload_namespace,
            args.run_id,
            args.run_suffix,
        )
        result = oc(
            "delete",
            "servicemonitor",
            name,
            "-n",
            args.workload_namespace,
            "--wait=true",
            "--timeout=120s",
            check=False,
            handled_secretly=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Cannot delete {args.workload_namespace}/ServiceMonitor/{name}; "
                "Receiver/PVC retained"
            )


def _receiver_namespace_state(
    args: Any,
) -> tuple[dict[str, Any] | None, str | None, bool, bool, bool | None]:
    namespace = _read_resource("namespace", args.receiver_namespace, allow_missing=True)
    if namespace is None:
        return None, None, False, False, None
    _require_managed(
        namespace,
        "Namespace",
        args.receiver_namespace,
        "",
        args.run_id,
        args.run_suffix,
    )
    annotations = (namespace.get("metadata") or {}).get("annotations") or {}
    phase = annotations.get(_TEARDOWN_PHASE_ANNOTATION)
    if phase is not None and phase not in _PHASES:
        raise RuntimeError(f"Namespace/{args.receiver_namespace} has an unknown teardown phase")
    queue_sent_value = annotations.get(_QUEUE_SENT_ANNOTATION)
    if queue_sent_value not in (None, "true", "false"):
        raise RuntimeError(f"Namespace/{args.receiver_namespace} has an invalid queue sent marker")
    queue_had_sent_samples = None if queue_sent_value is None else queue_sent_value == "true"
    if (
        phase is not None
        and _PHASES[phase] >= _PHASES["drained"]
        and queue_had_sent_samples is None
    ):
        raise RuntimeError(
            f"Namespace/{args.receiver_namespace} is missing its queue drain checkpoint; "
            "PVC retained"
        )
    if (namespace.get("metadata") or {}).get("deletionTimestamp"):
        if phase == "uploaded":
            return (
                namespace,
                phase,
                annotations.get(_SETUP_COMPLETE_ANNOTATION) == "true",
                False,
                queue_had_sent_samples,
            )
        raise RuntimeError(
            f"Namespace/{args.receiver_namespace} is being deleted before final upload was proven"
        )

    for kind, name in (
        ("statefulset", args.receiver_name),
        ("service", args.receiver_name),
        ("secret", f"{args.receiver_name}-objstore"),
        ("pod", f"{args.receiver_name}-0"),
    ):
        existing = _read_resource(kind, name, namespace=args.receiver_namespace, allow_missing=True)
        if existing is not None:
            _require_managed(
                existing,
                kind,
                name,
                args.receiver_namespace,
                args.run_id,
                args.run_suffix,
            )

    pvcs = _list_resources("pvc", args.receiver_namespace)
    for pvc in pvcs:
        name = (pvc.get("metadata") or {}).get("name")
        if not isinstance(name, str):
            raise RuntimeError(f"Malformed PVC in Namespace/{args.receiver_namespace}")
        _require_managed(pvc, "PVC", name, args.receiver_namespace, args.run_id, args.run_suffix)
        if name != f"data-{args.receiver_name}-0":
            raise RuntimeError(f"Namespace/{args.receiver_namespace} contains an unexpected PVC")
    pod = _read_resource(
        "pod",
        f"{args.receiver_name}-0",
        namespace=args.receiver_namespace,
        allow_missing=True,
    )
    may_have_data = bool(pvcs or pod)
    return (
        namespace,
        phase,
        annotations.get(_SETUP_COMPLETE_ANNOTATION) == "true",
        may_have_data,
        queue_had_sent_samples,
    )


def _configured_queue(
    namespace: str, configmap_name: str, section: str, queue_name: str, url: str
) -> bool:
    configmap = _read_resource("configmap", configmap_name, namespace=namespace, allow_missing=True)
    if configmap is None:
        return False
    raw = (configmap.get("data") or {}).get("config.yaml") or ""
    try:
        config = yaml.safe_load(raw) if raw else {}
    except yaml.YAMLError:
        raise RuntimeError(
            f"{namespace}/ConfigMap/{configmap_name} has invalid config.yaml"
        ) from None
    if not isinstance(config, dict):
        raise RuntimeError(f"{namespace}/ConfigMap/{configmap_name} config.yaml is not a mapping")
    section_value = config.get(section) or {}
    if not isinstance(section_value, dict):
        raise RuntimeError(f"{namespace}/ConfigMap/{configmap_name} has invalid {section}")
    queues = section_value.get("remoteWrite") or []
    if not isinstance(queues, list):
        raise RuntimeError(f"{namespace}/ConfigMap/{configmap_name} has invalid remoteWrite")
    matches = [item for item in queues if isinstance(item, dict) and item.get("name") == queue_name]
    if len(matches) > 1 or (matches and matches[0].get("url") != url):
        raise RuntimeError(f"{namespace}/ConfigMap/{configmap_name} has a conflicting run queue")
    return bool(matches)


def _cr_queue(namespace: str, prometheus_name: str, queue_name: str, url: str) -> bool:
    prometheus = _read_resource(
        "prometheus", prometheus_name, namespace=namespace, allow_missing=True
    )
    if prometheus is None:
        return False
    queues = (prometheus.get("spec") or {}).get("remoteWrite") or []
    if not isinstance(queues, list):
        raise RuntimeError(f"{namespace}/Prometheus/{prometheus_name} has invalid remoteWrite")
    matches = [item for item in queues if isinstance(item, dict) and item.get("name") == queue_name]
    if len(matches) > 1 or (matches and matches[0].get("url") != url):
        raise RuntimeError(f"{namespace}/Prometheus/{prometheus_name} has a conflicting run queue")
    return bool(matches)


def _queue_presence(args: Any) -> dict[tuple[str, str], tuple[bool, bool]]:
    url = _remote_write_url(args.receiver_name, args.receiver_namespace)
    return {
        (namespace, prometheus_name): (
            _configured_queue(namespace, configmap, section, args.queue_name, url),
            _cr_queue(namespace, prometheus_name, args.queue_name, url),
        )
        for namespace, configmap, section, prometheus_name in _STACK_CONFIG
    }


def _drain_queues_once(args: Any, ctx: Any) -> bool:
    if not hasattr(ctx, "drain_started"):
        ctx.drain_started = time.monotonic()
        ctx.drain_stacks = set()
        ctx.queue_stable = 0
        ctx.queue_previous_timestamps = None

    presence = _queue_presence(args)
    if (
        ctx.setup_complete
        and ctx.phase is None
        and not all(config and cr for config, cr in presence.values())
    ):
        raise RuntimeError(
            "A completed run is missing a named remoteWrite queue before drain; "
            "Receiver/PVC retained for recovery"
        )
    active_stacks = {stack for stack, (_, cr) in presence.items() if cr}
    ctx.drain_stacks.update(active_stacks)
    if not active_stacks:
        if ctx.queue_had_sent_samples is None:
            ctx.queue_had_sent_samples = False
        return True

    url = _remote_write_url(args.receiver_name, args.receiver_namespace)
    if _active_producers(args.workload_namespace):
        raise RuntimeError(
            "Workload producer appeared during remoteWrite drain; Receiver/PVC retained"
        )
    snapshots = {}
    for namespace, prometheus_name in active_stacks:
        stack = _stack_queue_snapshots(namespace, prometheus_name, args.queue_name, url)
        if stack is None or any(snapshot is None for snapshot in stack.values()):
            ctx.queue_stable = 0
            ctx.queue_previous_timestamps = None
            if time.monotonic() - ctx.drain_started >= args.queue_drain_timeout_seconds:
                raise RuntimeError(
                    f"Named remoteWrite queue {args.queue_name} did not drain on every "
                    "Prometheus replica; Receiver/PVC retained"
                )
            return False
        snapshots.update(stack)
    if any(
        snapshot.samples_failed > 0
        or snapshot.exemplars_failed > 0
        or snapshot.histograms_failed > 0
        or snapshot.unexpected_drops > 0
        for snapshot in snapshots.values()
    ):
        raise RuntimeError(
            "A named remoteWrite queue reports permanently failed or "
            "unexpectedly dropped data; Receiver/PVC retained"
        )
    if any(snapshot.highest_sent > 0 for snapshot in snapshots.values()):
        ctx.queue_had_sent_samples = True
    timestamps = {source: snapshot.highest_enqueued for source, snapshot in snapshots.items()}
    if all(snapshot.drained() for snapshot in snapshots.values()):
        ctx.queue_stable = (
            ctx.queue_stable + 1 if timestamps == ctx.queue_previous_timestamps else 1
        )
    else:
        ctx.queue_stable = 0
    ctx.queue_previous_timestamps = timestamps
    if ctx.queue_stable >= args.stable_polls:
        if ctx.queue_had_sent_samples is None:
            ctx.queue_had_sent_samples = False
        return True
    if time.monotonic() - ctx.drain_started >= args.queue_drain_timeout_seconds:
        raise RuntimeError(
            f"Named remoteWrite queue {args.queue_name} did not drain on every "
            "Prometheus replica; Receiver/PVC retained"
        )
    return False


def _set_phase(args: Any, phase: str) -> None:
    if _read_resource("namespace", args.receiver_namespace, allow_missing=True) is None:
        return

    def mutate(annotations: dict[str, str]) -> bool:
        current = annotations.get(_TEARDOWN_PHASE_ANNOTATION)
        if current is not None and current not in _PHASES:
            raise RuntimeError(f"Namespace/{args.receiver_namespace} has an unknown teardown phase")
        if current is not None and _PHASES[current] >= _PHASES[phase]:
            return False
        if current is not None and _PHASES[phase] != _PHASES[current] + 1:
            raise RuntimeError(
                f"Namespace/{args.receiver_namespace} has an invalid teardown transition"
            )
        annotations[_TEARDOWN_PHASE_ANNOTATION] = phase
        return True

    _update_namespace_annotations(args.receiver_namespace, args.run_id, args.run_suffix, mutate)


def _set_queue_sent_marker(args: Any, queue_had_sent_samples: bool) -> None:
    def mutate(annotations: dict[str, str]) -> bool:
        current = annotations.get(_QUEUE_SENT_ANNOTATION)
        if current not in (None, "true", "false"):
            raise RuntimeError(
                f"Namespace/{args.receiver_namespace} has an invalid queue sent marker"
            )
        value = "true" if current == "true" or queue_had_sent_samples else "false"
        if current == value:
            return False
        annotations[_QUEUE_SENT_ANNOTATION] = value
        return True

    _update_namespace_annotations(args.receiver_namespace, args.run_id, args.run_suffix, mutate)


def _remove_named_queue(args: Any) -> None:
    url = _remote_write_url(args.receiver_name, args.receiver_namespace)
    for namespace, configmap, section, _ in _STACK_CONFIG:

        def remove(
            config: dict[str, Any],
            *,
            namespace: str = namespace,
            configmap: str = configmap,
            section: str = section,
        ) -> bool:
            prometheus = config.get(section) or {}
            if not isinstance(prometheus, dict):
                raise RuntimeError(f"{namespace}/ConfigMap/{configmap} has invalid {section}")
            queues = prometheus.get("remoteWrite") or []
            if not isinstance(queues, list):
                raise RuntimeError(f"{namespace}/ConfigMap/{configmap} has invalid remoteWrite")
            matches = [
                entry
                for entry in queues
                if isinstance(entry, dict) and entry.get("name") == args.queue_name
            ]
            if len(matches) > 1 or (matches and matches[0].get("url") != url):
                raise RuntimeError(f"{namespace}/ConfigMap/{configmap} has a conflicting run queue")
            if not matches:
                return False
            prometheus["remoteWrite"] = [entry for entry in queues if entry is not matches[0]]
            config[section] = prometheus
            return True

        _update_configmap(namespace, configmap, remove)


def _queues_removed_once(args: Any, ctx: Any) -> bool:
    if not hasattr(ctx, "removal_started"):
        ctx.removal_started = time.monotonic()
        ctx.removal_stable = 0

    url = _remote_write_url(args.receiver_name, args.receiver_namespace)
    presence = _queue_presence(args)
    if any(config or cr for config, cr in presence.values()):
        ctx.removal_stable = 0
        if time.monotonic() - ctx.removal_started >= args.queue_removal_timeout_seconds:
            raise RuntimeError(
                f"Named remoteWrite queue {args.queue_name} was not removed; Receiver/PVC retained"
            )
        return False

    for namespace, _, _, prometheus_name in _STACK_CONFIG:
        stack_id = (namespace, prometheus_name)
        prometheus = _read_resource(
            "prometheus", prometheus_name, namespace=namespace, allow_missing=True
        )
        if prometheus is None:
            if (
                ctx.setup_complete
                or stack_id in ctx.drain_stacks
                or ctx.queue_had_sent_samples is True
            ):
                ctx.removal_stable = 0
                if time.monotonic() - ctx.removal_started >= args.queue_removal_timeout_seconds:
                    raise RuntimeError(
                        f"Named remoteWrite queue {args.queue_name} was not removed; "
                        "Receiver/PVC retained"
                    )
                return False
            continue
        stack = _stack_queue_snapshots(namespace, prometheus_name, args.queue_name, url)
        if stack is None:
            ctx.removal_stable = 0
            if time.monotonic() - ctx.removal_started >= args.queue_removal_timeout_seconds:
                raise RuntimeError(
                    f"Named remoteWrite queue {args.queue_name} was not removed; "
                    "Receiver/PVC retained"
                )
            return False
        if any(snapshot is not None for snapshot in stack.values()):
            ctx.removal_stable = 0
            if time.monotonic() - ctx.removal_started >= args.queue_removal_timeout_seconds:
                raise RuntimeError(
                    f"Named remoteWrite queue {args.queue_name} was not removed; "
                    "Receiver/PVC retained"
                )
            return False
    ctx.removal_stable += 1
    return ctx.removal_stable >= args.stable_polls


def _require_queues_absent(args: Any) -> None:
    if any(config or cr for config, cr in _queue_presence(args).values()):
        raise RuntimeError(
            "Named remoteWrite queue reappeared after teardown; Receiver/PVC retained"
        )


def _cut_receiver_service(args: Any) -> None:
    service = _read_resource(
        "service",
        args.receiver_name,
        namespace=args.receiver_namespace,
        allow_missing=True,
    )
    if service is None:
        return
    _require_managed(
        service,
        "Service",
        args.receiver_name,
        args.receiver_namespace,
        args.run_id,
        args.run_suffix,
    )
    result = oc(
        "delete",
        "service",
        args.receiver_name,
        "-n",
        args.receiver_namespace,
        "--wait=true",
        "--timeout=120s",
        check=False,
        handled_secretly=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot remove Receiver Service in {args.receiver_namespace}; Receiver/PVC retained"
        )


def _receiver_container_status(
    args: Any,
) -> tuple[str, str, int, bool, dict[str, Any]] | None:
    name = f"{args.receiver_name}-0"
    pod = _read_resource("pod", name, namespace=args.receiver_namespace, allow_missing=True)
    if pod is None:
        return None
    _require_managed(pod, "Pod", name, args.receiver_namespace, args.run_id, args.run_suffix)
    uid = (pod.get("metadata") or {}).get("uid")
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    matching = [status for status in statuses if status.get("name") == "thanos-receiver"]
    if not isinstance(uid, str) or len(matching) != 1:
        return None
    status = matching[0]
    container_id = status.get("containerID")
    restart_count = status.get("restartCount")
    if not isinstance(container_id, str) or not isinstance(restart_count, int):
        return None
    return uid, container_id, restart_count, status.get("ready") is True, status


def _k8s_timestamp(value: Any, description: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"Receiver {description} is unavailable; PVC retained")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError(f"Receiver {description} is invalid; PVC retained") from None
    if parsed.tzinfo is None:
        raise RuntimeError(f"Receiver {description} lacks a timezone; PVC retained")
    return parsed


def _read_shutdown_checkpoint(args: Any) -> dict[str, Any] | None:
    namespace = _read_resource("namespace", args.receiver_namespace, allow_missing=True)
    if namespace is None:
        return None
    _require_managed(
        namespace,
        "Namespace",
        args.receiver_namespace,
        "",
        args.run_id,
        args.run_suffix,
    )
    raw = ((namespace.get("metadata") or {}).get("annotations") or {}).get(
        _SHUTDOWN_CHECKPOINT_ANNOTATION
    )
    if raw is None:
        return None
    try:
        checkpoint = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError("Receiver shutdown checkpoint is invalid; PVC retained") from None
    if (
        not isinstance(checkpoint, dict)
        or not isinstance(checkpoint.get("pod_uid"), str)
        or not isinstance(checkpoint.get("container_id"), str)
        or isinstance(checkpoint.get("restart_count"), bool)
        or not isinstance(checkpoint.get("restart_count"), int)
        or checkpoint["restart_count"] < 0
    ):
        raise RuntimeError("Receiver shutdown checkpoint is invalid; PVC retained")
    _k8s_timestamp(checkpoint.get("signal_at"), "shutdown checkpoint time")
    return checkpoint


def _save_shutdown_checkpoint(args: Any, checkpoint: dict[str, Any]) -> None:
    serialized = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))

    def mutate(annotations: dict[str, str]) -> bool:
        if annotations.get(_TEARDOWN_PHASE_ANNOTATION) != "queues-removed":
            raise RuntimeError("Receiver shutdown requires completed queue removal; PVC retained")
        if annotations.get(_SHUTDOWN_CHECKPOINT_ANNOTATION) == serialized:
            return False
        annotations[_SHUTDOWN_CHECKPOINT_ANNOTATION] = serialized
        return True

    _update_namespace_annotations(args.receiver_namespace, args.run_id, args.run_suffix, mutate)


def _verify_previous_shutdown_logs(args: Any, *, queue_had_sent_samples: bool | None) -> None:
    pod_name = f"{args.receiver_name}-0"
    result = oc(
        "logs",
        pod_name,
        "-n",
        args.receiver_namespace,
        "-c",
        "thanos-receiver",
        "--previous",
        "--tail=1000",
        check=False,
        handled_secretly=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Cannot read previous Receiver shutdown logs; PVC retained")
    logs = result.stdout
    shutdown_index = logs.rfind("shutting down storage")
    shutdown_logs = logs[shutdown_index:] if shutdown_index >= 0 else ""
    markers = (
        "storage is flushed successfully",
        "storage is closed",
        "uploading the final cut block before exiting",
        "the final cut block was uploaded",
    )
    position = 0
    for marker in markers:
        position = shutdown_logs.find(marker, position)
        if position < 0:
            raise RuntimeError(
                "Receiver final TSDB flush or S3 upload was not proven; PVC retained"
            )
        position += len(marker)
    if any(
        marker in shutdown_logs
        for marker in (
            "failed to flush storage",
            "flushing TSDB failed; not ready",
            "failed to close storage",
            "the final upload failed",
        )
    ):
        raise RuntimeError("Receiver final TSDB flush or S3 upload was not proven; PVC retained")
    if "flushing TSDB" not in shutdown_logs and queue_had_sent_samples is not False:
        # MultiTSDB.Flush logs success even when no tenant was loaded. That is
        # safe only when the Prometheus queues proved they sent no samples.
        raise RuntimeError(
            "Receiver had no tenant to flush but an empty run was not proven; PVC retained"
        )


def _shutdown_completed(
    args: Any, checkpoint: dict[str, Any], *, queue_had_sent_samples: bool | None
) -> bool:
    container_status = _receiver_container_status(args)
    if container_status is None:
        return False
    uid, container_id, restart_count, ready, status = container_status
    if uid != checkpoint["pod_uid"] or restart_count > checkpoint["restart_count"] + 1:
        raise RuntimeError("Receiver Pod changed unexpectedly during final upload; PVC retained")
    if restart_count == checkpoint["restart_count"]:
        return False
    if container_id == checkpoint["container_id"] or not ready:
        return False
    terminated = (status.get("lastState") or {}).get("terminated") or {}
    if (
        terminated.get("containerID") != checkpoint["container_id"]
        or terminated.get("exitCode") != 0
        or _k8s_timestamp(terminated.get("finishedAt"), "termination time")
        < _k8s_timestamp(checkpoint["signal_at"], "shutdown checkpoint time")
    ):
        raise RuntimeError(
            "Receiver did not complete the checkpointed graceful shutdown; PVC retained"
        )
    _verify_previous_shutdown_logs(args, queue_had_sent_samples=queue_had_sent_samples)
    return True


def _initiate_receiver_shutdown(args: Any, ctx: Any) -> None:
    statefulset = _read_resource(
        "statefulset",
        args.receiver_name,
        namespace=args.receiver_namespace,
        allow_missing=True,
    )
    if statefulset is None:
        raise RuntimeError(
            f"Receiver StatefulSet {args.receiver_namespace}/{args.receiver_name} "
            "is missing; PVC retained"
        )
    _require_managed(
        statefulset,
        "StatefulSet",
        args.receiver_name,
        args.receiver_namespace,
        args.run_id,
        args.run_suffix,
    )
    previous = _read_shutdown_checkpoint(args)
    if previous is not None:
        try:
            if _shutdown_completed(
                args,
                previous,
                queue_had_sent_samples=ctx.queue_had_sent_samples,
            ):
                ctx.upload_verified = True
                return
        except RuntimeError:
            # A failed prior attempt may be retried after fixing S3. The new
            # shutdown receives a fresh checkpoint and must prove its own logs.
            pass

    container_status = _receiver_container_status(args)
    if container_status is None:
        return
    pod_uid, container_id, restart_count, ready, _ = container_status
    if not ready:
        if previous is not None and pod_uid == previous["pod_uid"]:
            ctx.shutdown_checkpoint = previous
        return

    checkpoint = {
        "pod_uid": pod_uid,
        "container_id": container_id,
        "restart_count": restart_count,
        "signal_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    _save_shutdown_checkpoint(args, checkpoint)
    ctx.shutdown_checkpoint = checkpoint

    # PID 1 is Thanos in the v0.35 Receiver image. Kubernetes restarts this
    # container in the same StatefulSet Pod, preserving --previous logs.
    result = oc(
        "exec",
        "-n",
        args.receiver_namespace,
        f"{args.receiver_name}-0",
        "-c",
        "thanos-receiver",
        "--",
        "sh",
        "-c",
        "kill -TERM 1",
        check=False,
        timeout_seconds=30,
        handled_secretly=True,
    )
    if "forbidden" in result.stderr.lower() or "unauthorized" in result.stderr.lower():
        raise PermissionError(
            f"Cannot signal Receiver Pod in {args.receiver_namespace}; PVC retained"
        )
    ctx.shutdown_signalled = True


def _is_generated_service_account_secret(
    secret: dict[str, Any], service_accounts: dict[str, dict[str, Any]]
) -> bool:
    """Allow only OpenShift-generated pull/token secrets for built-in SAs."""

    metadata = secret.get("metadata") or {}
    name = metadata.get("name")
    annotations = metadata.get("annotations") or {}
    secret_type = secret.get("type")
    for service_account_name, service_account in service_accounts.items():
        sa_metadata = service_account.get("metadata") or {}
        sa_uid = sa_metadata.get("uid")
        sa_annotations = sa_metadata.get("annotations") or {}
        image_pull_refs = {
            item.get("name")
            for item in service_account.get("imagePullSecrets") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        internal_pull_secret = sa_annotations.get("openshift.io/internal-registry-pull-secret-ref")
        if (
            isinstance(name, str)
            and name == internal_pull_secret
            and name in image_pull_refs
            and name.startswith(f"{service_account_name}-dockercfg-")
            and secret_type in ("kubernetes.io/dockercfg", "kubernetes.io/dockerconfigjson")
        ):
            return True

        token_refs = {
            item.get("name")
            for item in service_account.get("secrets") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if (
            isinstance(name, str)
            and name in token_refs
            and name.startswith(f"{service_account_name}-token-")
            and secret_type == "kubernetes.io/service-account-token"
            and annotations.get("kubernetes.io/service-account.name") == service_account_name
            and isinstance(sa_uid, str)
            and annotations.get("kubernetes.io/service-account.uid") == sa_uid
        ):
            return True
    return False


def _delete_run_namespace(args: Any) -> None:
    namespace = _read_resource("namespace", args.receiver_namespace, allow_missing=True)
    if namespace is None:
        return
    _require_managed(
        namespace,
        "Namespace",
        args.receiver_namespace,
        "",
        args.run_id,
        args.run_suffix,
    )
    phase = ((namespace.get("metadata") or {}).get("annotations") or {}).get(
        _TEARDOWN_PHASE_ANNOTATION
    )
    if phase != "uploaded":
        raise RuntimeError(
            f"Namespace/{args.receiver_namespace} lacks final upload proof; PVC retained"
        )
    # This namespace is generated from the exact run ID and contains only the
    # Receiver. Check its data-bearing and controller objects before deleting it.
    for kind, expected in (
        ("pod", {f"{args.receiver_name}-0"}),
        ("statefulset", {args.receiver_name}),
        ("pvc", {f"data-{args.receiver_name}-0"}),
        ("service", {args.receiver_name}),
    ):
        for item in _list_resources(kind, args.receiver_namespace):
            name = (item.get("metadata") or {}).get("name")
            if not isinstance(name, str) or name not in expected:
                raise RuntimeError(
                    f"Namespace/{args.receiver_namespace} contains an unexpected "
                    f"{kind}; refusing namespace deletion"
                )
            _require_managed(
                item,
                kind,
                name,
                args.receiver_namespace,
                args.run_id,
                args.run_suffix,
            )

    service_accounts: dict[str, dict[str, Any]] = {}
    for service_account in _list_resources("serviceaccount", args.receiver_namespace):
        name = (service_account.get("metadata") or {}).get("name")
        if not isinstance(name, str) or name not in {"default", "builder", "deployer", "pipeline"}:
            raise RuntimeError(
                f"Namespace/{args.receiver_namespace} contains an unexpected "
                "serviceaccount; refusing namespace deletion"
            )
        service_accounts[name] = service_account

    for secret in _list_resources("secret", args.receiver_namespace):
        name = (secret.get("metadata") or {}).get("name")
        if name == f"{args.receiver_name}-objstore":
            _require_managed(
                secret,
                "Secret",
                name,
                args.receiver_namespace,
                args.run_id,
                args.run_suffix,
            )
        elif not _is_generated_service_account_secret(secret, service_accounts):
            raise RuntimeError(
                f"Namespace/{args.receiver_namespace} contains an unexpected "
                "Secret; refusing namespace deletion"
            )

    for kind in (
        "deployment",
        "daemonset",
        "job",
        "replicaset",
        "replicationcontroller",
        "cronjob",
        "deploymentconfig",
        "inferenceservice",
        "llminferenceservice",
    ):
        if _list_resources(kind, args.receiver_namespace):
            raise RuntimeError(
                f"Namespace/{args.receiver_namespace} contains an unexpected "
                f"{kind}; refusing namespace deletion"
            )
    result = oc(
        "delete",
        "namespace",
        args.receiver_namespace,
        "--wait=true",
        "--timeout=300s",
        check=False,
        handled_secretly=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not finish deleting Namespace/{args.receiver_namespace}; "
            "final upload was verified and retry is safe"
        )


@task
def inspect_teardown_state(args, ctx):
    """Load the run-owned Receiver state and durable cleanup checkpoints."""

    namespace, phase, setup_complete, may_have_data, queue_had_sent_samples = (
        _receiver_namespace_state(args)
    )
    if namespace is not None and (namespace.get("metadata") or {}).get("deletionTimestamp"):
        ctx.namespace_deleting = True
    else:
        ctx.namespace_deleting = False
    ctx.receiver_namespace_exists = namespace is not None
    ctx.phase = phase
    ctx.setup_complete = setup_complete
    ctx.may_have_data = may_have_data
    ctx.queue_had_sent_samples = queue_had_sent_samples
    ctx.initial_queue_presence = _queue_presence(args)
    ctx.drain_stacks = {stack for stack, (_, cr) in ctx.initial_queue_presence.items() if cr}
    ctx.has_any_queue = any(config or cr for config, cr in ctx.initial_queue_presence.values())
    return f"Teardown phase for {args.receiver_namespace}: {phase or 'not started'}"


@retry(attempts=_MAX_RETRY_ATTEMPTS, delay=_RETRY_DELAY_SECONDS, backoff=1.0)
@task
def wait_for_workload_stop(args, ctx):
    """Wait until the selected workload namespace has no active producers."""

    if ctx.phase == "uploaded" or ctx.namespace_deleting:
        return "Final upload is already recorded"
    if not ctx.receiver_namespace_exists and not ctx.has_any_queue:
        return "No run Receiver or remote-write queue needs draining"

    if not hasattr(ctx, "workload_stop_started"):
        ctx.workload_stop_started = time.monotonic()
        ctx.workload_stop_stable = 0
    active = _active_producers(args.workload_namespace)
    ctx.workload_stop_stable = ctx.workload_stop_stable + 1 if not active else 0
    if ctx.workload_stop_stable >= args.stable_polls:
        return "Workload namespace is quiescent"
    if time.monotonic() - ctx.workload_stop_started >= args.workload_stop_timeout_seconds:
        raise RuntimeError(
            f"Workload namespace {args.workload_namespace} still has producer "
            "Pods or controllers; Receiver/PVC retained"
        )
    return False, f"Workload producers are still active: {active}"


@task
def remove_service_monitors(args, ctx):
    """Remove this run's ServiceMonitors after workload stop is proven."""

    _delete_service_monitors(args)
    return f"Removed run ServiceMonitors from {args.workload_namespace}"


@retry(attempts=_MAX_RETRY_ATTEMPTS, delay=_RETRY_DELAY_SECONDS, backoff=1.0)
@task
def drain_remote_write_queues(args, ctx):
    """Wait until all live named Prometheus queues have drained."""

    if ctx.phase is not None:
        return f"Queues were already drained in phase {ctx.phase}"
    if not ctx.receiver_namespace_exists and not ctx.has_any_queue:
        return "No run Receiver or remote-write queue needs draining"
    drained = _drain_queues_once(args, ctx)
    if drained:
        return f"Named remote-write queue {args.queue_name} is drained"
    return False, f"Waiting for named queue {args.queue_name} to drain"


@task
def mark_queues_drained(args, ctx):
    """Persist the drain result before changing either Prometheus ConfigMap."""

    if ctx.phase is not None or not ctx.receiver_namespace_exists:
        return "Queue drain phase was already recorded or no Receiver exists"
    if ctx.queue_had_sent_samples is None:
        raise RuntimeError(
            "Could not determine whether the Receiver accepted samples; PVC retained"
        )
    _set_queue_sent_marker(args, ctx.queue_had_sent_samples)
    _set_phase(args, "drained")
    ctx.phase = "drained"
    return "Persisted drained queue checkpoint"


@task
def remove_remote_write_queues(args, ctx):
    """Remove only this run's named queues from the monitoring ConfigMaps."""

    if ctx.phase == "uploaded":
        _require_queues_absent(args)
        return "Final upload is already recorded"
    if ctx.phase in (None, "drained"):
        _remove_named_queue(args)
        return f"Removed named remote-write queue {args.queue_name}"
    _require_queues_absent(args)
    return "Named remote-write queues are already removed"


@retry(attempts=_MAX_RETRY_ATTEMPTS, delay=_RETRY_DELAY_SECONDS, backoff=1.0)
@task
def wait_for_remote_write_removal(args, ctx):
    """Confirm that the removed queue is gone from both Prometheus stacks."""

    if ctx.phase not in (None, "drained"):
        return f"Queue removal was already recorded in phase {ctx.phase}"
    if not ctx.receiver_namespace_exists and not ctx.has_any_queue:
        return "No run Receiver or remote-write queue needs removal"
    if _queues_removed_once(args, ctx):
        return f"Named remote-write queue {args.queue_name} disappeared"
    return False, f"Waiting for named queue {args.queue_name} to disappear"


@task
def mark_queues_removed(args, ctx):
    """Persist that both Prometheus stacks stopped using this Receiver."""

    if ctx.phase in (None, "drained") and ctx.receiver_namespace_exists:
        _set_phase(args, "queues-removed")
        ctx.phase = "queues-removed"
        return "Persisted queue removal checkpoint"
    if ctx.phase == "uploaded":
        _require_queues_absent(args)
    return "Queue removal phase was already recorded or no Receiver exists"


@task
def cut_receiver_service(args, ctx):
    """Stop new remote writes after both Prometheus stacks have removed the queue."""

    if ctx.phase == "uploaded" or not ctx.receiver_namespace_exists:
        return "No Receiver Service needs removal"
    _cut_receiver_service(args)
    return f"Removed Receiver Service from {args.receiver_namespace}"


@retry(attempts=_MAX_RETRY_ATTEMPTS, delay=_RETRY_DELAY_SECONDS, backoff=1.0)
@task
def flush_and_verify_upload(args, ctx):
    """Restart Thanos gracefully and verify its final flush and S3 upload."""

    if ctx.phase == "uploaded":
        return "Final S3 upload is already recorded"
    if not ctx.receiver_namespace_exists or not ctx.may_have_data:
        return "No Receiver data needs a final flush"
    if ctx.phase != "queues-removed":
        raise RuntimeError("Receiver shutdown requires completed queue removal; PVC retained")

    if not hasattr(ctx, "shutdown_started"):
        ctx.shutdown_started = time.monotonic()
        ctx.shutdown_checkpoint = _read_shutdown_checkpoint(args)

    if getattr(ctx, "upload_verified", False):
        return "Receiver final flush and S3 upload verified"
    checkpoint = getattr(ctx, "shutdown_checkpoint", None)
    if not getattr(ctx, "shutdown_signalled", False):
        if checkpoint is not None:
            try:
                if _shutdown_completed(
                    args,
                    checkpoint,
                    queue_had_sent_samples=ctx.queue_had_sent_samples,
                ):
                    ctx.upload_verified = True
                    return "Receiver final flush and S3 upload verified"
            except RuntimeError:
                # Preserve the PVC and start a fresh checkpointed shutdown.
                pass
        _initiate_receiver_shutdown(args, ctx)
        if getattr(ctx, "upload_verified", False):
            return "Receiver final flush and S3 upload verified"
        if not getattr(ctx, "shutdown_signalled", False):
            if time.monotonic() - ctx.shutdown_started >= args.receiver_shutdown_timeout_seconds:
                raise RuntimeError(
                    "Receiver graceful shutdown and final upload timed out; PVC retained"
                )
            return False, "Waiting for a Ready Receiver before final flush"
        checkpoint = ctx.shutdown_checkpoint

    if checkpoint is not None and _shutdown_completed(
        args,
        checkpoint,
        queue_had_sent_samples=ctx.queue_had_sent_samples,
    ):
        ctx.upload_verified = True
        return "Receiver final flush and S3 upload verified"
    if time.monotonic() - ctx.shutdown_started >= args.receiver_shutdown_timeout_seconds:
        raise RuntimeError("Receiver graceful shutdown and final upload timed out; PVC retained")
    return False, "Waiting for Receiver restart and final upload logs"


@task
def mark_upload_verified(args, ctx):
    """Record upload proof before Forge deletes the Receiver PVC."""

    if ctx.phase == "uploaded" or not ctx.receiver_namespace_exists:
        return "Final upload phase was already recorded or no Receiver exists"
    if ctx.may_have_data and not getattr(ctx, "upload_verified", False):
        raise RuntimeError("Receiver final upload has not been verified; PVC retained")
    _set_phase(args, "uploaded")
    ctx.phase = "uploaded"
    return "Persisted final upload checkpoint"


@task
def delete_receiver_namespace(args, ctx):
    """Delete the Receiver namespace and PVC after the upload checkpoint."""

    if not ctx.receiver_namespace_exists:
        return "No run Receiver namespace exists"
    _delete_run_namespace(args)
    return f"Deleted run Receiver namespace {args.receiver_namespace}"


if __name__ == "__main__":
    run.main()
