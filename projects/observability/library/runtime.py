"""Run-scoped observability settings and orchestration-facing lifecycle API."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from pathlib import Path
from typing import Any

import yaml

_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_CLUSTER_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
_PROMETHEUS_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WORKLOAD_LOCK_ANNOTATION = "forge.openshift.io/observability-run-id"


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"observability.{key} must be a mapping")
    return value


def _required_string(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"observability.{key} must be a nonempty string")
    return value


def _dns_label(value: Any, key: str) -> str:
    name = _required_string(value, key)
    if len(name) > 63 or not _DNS_LABEL.fullmatch(name):
        raise ValueError(f"observability.{key} must be a Kubernetes DNS label")
    return name


def _run_id(value: Any) -> str:
    run_id = _required_string(value, "run_id")
    if len(run_id) > 253 or any(
        len(part) > 63 or not _DNS_LABEL.fullmatch(part) for part in run_id.split(".")
    ):
        raise ValueError("observability.run_id must be a Kubernetes DNS name")
    return run_id


def _boolean(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"observability.{key} must be a boolean")
    return value


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"observability.{key} must be a positive integer")
    return value


def _optional_positive_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, key)


def _string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"observability.{key} must be a nonempty list")
    return [_required_string(item, f"{key}[{index}]") for index, item in enumerate(value)]


def _relabel_component_regex(value: Any, key: str) -> str:
    pattern = _required_string(value, key)
    if ";" in pattern or "\n" in pattern:
        raise ValueError(f"observability.{key} cannot contain a relabel separator")
    unanchored = pattern.removeprefix("^").removesuffix("$")
    if "^" in unanchored or "$" in unanchored:
        raise ValueError(f"observability.{key} supports anchors only at the outer edges")
    try:
        re.compile(pattern)
    except re.error:
        raise ValueError(f"observability.{key} is invalid") from None
    return pattern


def resolve_run_id(settings: dict) -> str:
    """Resolve one stable Forge run ID; explicit config wins over Fournos state."""

    settings = _mapping(settings, "")
    if settings.get("run_id") is not None:
        configured = _run_id(settings["run_id"])
        environment_id = os.environ.get("FJOB_NAME")
        if environment_id and configured != _run_id(environment_id):
            raise ValueError("observability.run_id must equal FJOB_NAME in a Fournos job")
        return configured

    environment_id = os.environ.get("FJOB_NAME")
    if environment_id:
        return _run_id(environment_id)

    # Fournos can set ci_job.fjob_name before FJOB_NAME appears in a phase pod.
    # A local run must provide observability.run_id explicitly.
    from projects.core.library import config

    configured_id = config.project.get_config("ci_job.fjob_name", None, print=False, warn=False)
    if configured_id:
        return _run_id(configured_id)
    raise ValueError(
        "observability.run_id is required outside a Fournos job (or set FJOB_NAME/ci_job.fjob_name)"
    )


def run_resource_suffix(run_id: str) -> str:
    """Return the stable short identifier used by this run's Kubernetes objects."""

    validated = _run_id(run_id)
    return hashlib.sha256(validated.encode("utf-8")).hexdigest()[:12]


def _run_scoped_name(base: str, suffix: str, key: str) -> str:
    name = f"{base}-{suffix}"
    if len(name) > 63:
        raise ValueError(f"observability.{key} is too long with its run suffix")
    return name


def _service_monitors(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("observability.service_monitors must be a list")

    monitors: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(value):
        key = f"service_monitors[{index}]"
        spec = _mapping(raw, key)
        name = _dns_label(spec.get("name"), f"{key}.name")
        if name in seen_names:
            raise ValueError(f"Duplicate observability ServiceMonitor name: {name}")
        seen_names.add(name)

        selector = _mapping(spec.get("selector"), f"{key}.selector")
        if not selector or not all(
            isinstance(label_key, str)
            and label_key
            and isinstance(label_value, str)
            and label_value
            for label_key, label_value in selector.items()
        ):
            raise ValueError(f"observability.{key}.selector must contain label strings")

        port = _required_string(spec.get("port"), f"{key}.port")
        path = _required_string(spec.get("path", "/metrics"), f"{key}.path")
        if not path.startswith("/"):
            raise ValueError(f"observability.{key}.path must start with /")
        interval = _required_string(spec.get("interval", "15s"), f"{key}.interval")
        scrape_timeout = _required_string(
            spec.get("scrape_timeout", "10s"), f"{key}.scrape_timeout"
        )
        scheme = _required_string(spec.get("scheme", "http"), f"{key}.scheme")
        if scheme not in {"http", "https"}:
            raise ValueError(f"observability.{key}.scheme must be http or https")
        monitors.append(
            {
                "name": name,
                "selector": dict(selector),
                "port": port,
                "path": path,
                "interval": interval,
                "scrape_timeout": scrape_timeout,
                "scheme": scheme,
            }
        )
    return monitors


def _validate_fournos_pipeline(run_id: str) -> None:
    """Fournos pipelines without post-cleanup cannot own a temporary Receiver."""

    if os.environ.get("FOURNOS_CI") != "true":
        return
    artifact_dir = os.environ.get("FORGE_BASE_ARTIFACT_DIR") or os.environ.get("ARTIFACT_DIR")
    if not artifact_dir:
        raise ValueError("ARTIFACT_DIR is required for Fournos observability validation")
    phase_root = Path(artifact_dir)
    metadata_path = phase_root / "000__ci_metadata" / "fournos_fjob.yaml"
    if not metadata_path.is_file():
        metadata_path = phase_root / "fournos_fjob.yaml"
    try:
        fjob = yaml.safe_load(metadata_path.read_text())
    except (OSError, yaml.YAMLError, UnicodeError):
        # FournosJob YAML may contain sensitive spec.env values. Never echo it.
        raise ValueError("Cannot read valid FournosJob metadata for observability") from None
    if not isinstance(fjob, dict):
        raise ValueError("Cannot read valid FournosJob metadata for observability")
    metadata = fjob.get("metadata") or {}
    spec = fjob.get("spec") or {}
    if not isinstance(metadata, dict) or metadata.get("name") != run_id:
        raise ValueError("observability.run_id must equal this FournosJob name")
    if not isinstance(spec, dict) or spec.get("pipeline") != "forge-full":
        raise ValueError(
            "Per-run observability requires Fournos spec.pipeline: forge-full "
            "so post-cleanup tears down the Receiver"
        )


def _validated_settings(
    settings: dict, *, workload_namespace: str, run_id: str
) -> dict[str, Any] | None:
    settings = _mapping(settings, "")
    if not _boolean(settings.get("enabled", False), "enabled"):
        return None

    run_id = _run_id(run_id)
    environment_id = os.environ.get("FJOB_NAME")
    if environment_id and run_id != _run_id(environment_id):
        raise ValueError("observability.run_id must equal FJOB_NAME in a Fournos job")
    _validate_fournos_pipeline(run_id)
    workload_namespace = _dns_label(workload_namespace, "workload_namespace")
    suffix = run_resource_suffix(run_id)
    cluster_label = _required_string(settings.get("cluster_label"), "cluster_label")
    if not _CLUSTER_LABEL.fullmatch(cluster_label):
        raise ValueError(
            "observability.cluster_label must contain only letters, digits, dots, "
            "underscores, or hyphens"
        )

    receiver = _mapping(settings.get("receiver"), "receiver")
    if "mode" in receiver:
        raise ValueError(
            "observability.receiver.mode is no longer supported; each run creates its own Receiver"
        )
    receiver_base_namespace = _dns_label(receiver.get("namespace"), "receiver.namespace")
    receiver_namespace = _run_scoped_name(receiver_base_namespace, suffix, "receiver.namespace")
    if receiver_namespace == workload_namespace:
        raise ValueError("observability receiver namespace must differ from workload_namespace")
    receiver_name = _dns_label(receiver.get("name"), "receiver.name")
    if len(f"{receiver_name}-objstore") > 63:
        raise ValueError("observability.receiver.name is too long for its Secret")
    receiver_image = _required_string(receiver.get("image"), "receiver.image")
    receiver_pvc_size = _required_string(receiver.get("pvc_size"), "receiver.pvc_size")
    receiver_retention = _required_string(receiver.get("retention", "6h"), "receiver.retention")
    receiver_out_of_order_window = receiver.get("out_of_order_window")
    if receiver_out_of_order_window is not None:
        receiver_out_of_order_window = _required_string(
            receiver_out_of_order_window, "receiver.out_of_order_window"
        )
    receiver_storage_class = receiver.get("storage_class")
    if receiver_storage_class is not None:
        receiver_storage_class = _required_string(receiver_storage_class, "receiver.storage_class")
    receiver_run_as_user = _optional_positive_int(
        receiver.get("run_as_user"), "receiver.run_as_user"
    )
    receiver_fs_group = _optional_positive_int(receiver.get("fs_group"), "receiver.fs_group")
    receiver_resources = receiver.get("resources")
    if receiver_resources is not None:
        receiver_resources = _mapping(receiver_resources, "receiver.resources")
    receiver_shutdown_grace_seconds = _positive_int(
        receiver.get("shutdown_grace_seconds", 1200),
        "receiver.shutdown_grace_seconds",
    )
    vault_spec = _mapping(receiver.get("objstore_vault"), "receiver.objstore_vault")
    vault_name = _required_string(vault_spec.get("name"), "receiver.objstore_vault.name")
    vault_content = _required_string(vault_spec.get("content"), "receiver.objstore_vault.content")

    monitoring = _mapping(settings.get("monitoring", {}), "monitoring")
    if not _boolean(monitoring.get("enabled", True), "monitoring.enabled"):
        raise ValueError("observability.monitoring.enabled must be true for a run-scoped Receiver")
    queue_base = _dns_label(monitoring.get("queue_name"), "monitoring.queue_name")
    queue_name = _run_scoped_name(queue_base, suffix, "monitoring.queue_name")
    configured_regex = monitoring.get("namespace_regex")
    if configured_regex not in (None, workload_namespace, f"^{workload_namespace}$"):
        raise ValueError(
            "observability.monitoring.namespace_regex must select exactly workload_namespace"
        )
    namespace_regex = workload_namespace
    container_names = [
        _dns_label(name, f"monitoring.container_names[{index}]")
        for index, name in enumerate(
            _string_list(monitoring.get("container_names"), "monitoring.container_names")
        )
    ]
    forward_gpu_metrics = _boolean(
        monitoring.get("forward_gpu_metrics", False), "monitoring.forward_gpu_metrics"
    )
    gpu_namespaces: list[str] = []
    gpu_workload_label: str | None = None
    if forward_gpu_metrics:
        gpu_namespaces = [
            _dns_label(name, f"monitoring.gpu_namespaces[{index}]")
            for index, name in enumerate(
                _string_list(monitoring.get("gpu_namespaces"), "monitoring.gpu_namespaces")
            )
        ]
        gpu_workload_label = _required_string(
            monitoring.get("gpu_workload_label", "exported_namespace"),
            "monitoring.gpu_workload_label",
        )
        if not _PROMETHEUS_LABEL.fullmatch(gpu_workload_label):
            raise ValueError(
                "observability.monitoring.gpu_workload_label must be a Prometheus label name"
            )
    container_metric_regex = _relabel_component_regex(
        monitoring.get("container_metric_regex", "container_(cpu|memory)_.*"),
        "monitoring.container_metric_regex",
    )
    gpu_metric_regex = _relabel_component_regex(
        monitoring.get("gpu_metric_regex", "(DCGM_|gpu_).*"),
        "monitoring.gpu_metric_regex",
    )

    teardown = _mapping(settings.get("teardown", {}), "teardown")
    stable_polls = _positive_int(teardown.get("stable_polls", 3), "teardown.stable_polls")
    workload_stop_timeout_seconds = _positive_int(
        teardown.get("workload_stop_timeout_seconds", 300),
        "teardown.workload_stop_timeout_seconds",
    )
    queue_drain_timeout_seconds = _positive_int(
        teardown.get("queue_drain_timeout_seconds", 600),
        "teardown.queue_drain_timeout_seconds",
    )
    queue_removal_timeout_seconds = _positive_int(
        teardown.get("queue_removal_timeout_seconds", 300),
        "teardown.queue_removal_timeout_seconds",
    )
    receiver_shutdown_timeout_seconds = _positive_int(
        teardown.get("receiver_shutdown_timeout_seconds", 900),
        "teardown.receiver_shutdown_timeout_seconds",
    )
    if receiver_shutdown_timeout_seconds >= receiver_shutdown_grace_seconds:
        raise ValueError(
            "observability.teardown.receiver_shutdown_timeout_seconds must be "
            "less than receiver.shutdown_grace_seconds"
        )
    service_monitors = _service_monitors(settings.get("service_monitors", []))

    return {
        "run_id": run_id,
        "run_suffix": suffix,
        "cluster_label": cluster_label,
        "workload_namespace": workload_namespace,
        "receiver_namespace": receiver_namespace,
        "receiver_name": receiver_name,
        "receiver_image": receiver_image,
        "receiver_storage_class": receiver_storage_class,
        "receiver_pvc_size": receiver_pvc_size,
        "receiver_retention": receiver_retention,
        "receiver_out_of_order_window": receiver_out_of_order_window,
        "receiver_run_as_user": receiver_run_as_user,
        "receiver_fs_group": receiver_fs_group,
        "receiver_resources": receiver_resources,
        "receiver_shutdown_grace_seconds": receiver_shutdown_grace_seconds,
        "vault_name": vault_name,
        "vault_content": vault_content,
        "queue_name": queue_name,
        "namespace_regex": namespace_regex,
        "container_names": container_names,
        "container_metric_regex": container_metric_regex,
        "forward_gpu_metrics": forward_gpu_metrics,
        "gpu_namespaces": gpu_namespaces,
        "gpu_workload_label": gpu_workload_label,
        "gpu_metric_regex": gpu_metric_regex,
        "service_monitors": service_monitors,
        "stable_polls": stable_polls,
        "workload_stop_timeout_seconds": workload_stop_timeout_seconds,
        "queue_drain_timeout_seconds": queue_drain_timeout_seconds,
        "queue_removal_timeout_seconds": queue_removal_timeout_seconds,
        "receiver_shutdown_timeout_seconds": receiver_shutdown_timeout_seconds,
    }


def validate_observability_run(settings: dict, *, workload_namespace: str, run_id: str) -> None:
    """Pure early validation to run before any host cluster mutations."""

    _validated_settings(settings, workload_namespace=workload_namespace, run_id=run_id)


def get_workload_namespace_owner(workload_namespace: str) -> str | None:
    """Return the observability run that currently owns a workload namespace."""

    from projects.core.dsl.utils.k8s import oc_get_json

    namespace = oc_get_json(
        "namespace",
        name=_dns_label(workload_namespace, "workload_namespace"),
        ignore_not_found=True,
        handled_secretly=True,
    )
    if namespace is None:
        return None
    annotations = (namespace.get("metadata") or {}).get("annotations") or {}
    owner = annotations.get(_WORKLOAD_LOCK_ANNOTATION)
    if owner is not None and not isinstance(owner, str):
        raise RuntimeError(
            f"Namespace/{workload_namespace} has an invalid observability owner marker"
        )
    return owner


def _update_workload_namespace_lock(workload_namespace: str, run_id: str, *, claim: bool) -> None:
    from projects.core.dsl.utils.k8s import oc, oc_get_json

    namespace_name = _dns_label(workload_namespace, "workload_namespace")
    run_id = _run_id(run_id)
    action = "claim" if claim else "release"

    for _ in range(5):
        namespace = oc_get_json(
            "namespace",
            name=namespace_name,
            ignore_not_found=True,
            handled_secretly=True,
        )
        if namespace is None:
            if claim:
                raise RuntimeError(
                    f"Cannot claim observability namespace lock; "
                    f"Namespace/{namespace_name} does not exist"
                )
            return

        manifest = copy.deepcopy(namespace)
        metadata = manifest.setdefault("metadata", {})
        annotations = metadata.get("annotations") or {}
        owner = annotations.get(_WORKLOAD_LOCK_ANNOTATION)
        if claim and owner == run_id:
            return
        if claim and owner is not None:
            raise RuntimeError(
                f"Namespace/{namespace_name} is already claimed by another "
                "observability run; Forge runs sharing a workload namespace "
                "must run one at a time"
            )
        if not claim and owner is None:
            return
        if not claim and owner != run_id:
            raise RuntimeError(
                f"Cannot release Namespace/{namespace_name} observability lock "
                "because another run owns it"
            )

        metadata.pop("managedFields", None)
        manifest.pop("status", None)
        annotations = dict(annotations)
        if claim:
            annotations[_WORKLOAD_LOCK_ANNOTATION] = run_id
        else:
            annotations.pop(_WORKLOAD_LOCK_ANNOTATION, None)
        if annotations:
            metadata["annotations"] = annotations
        else:
            metadata.pop("annotations", None)

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
        if "forbidden" in stderr or "unauthorized" in stderr:
            raise PermissionError(
                f"Cannot {action} observability lock on Namespace/{namespace_name}; RBAC is required"
            )
        if "conflict" in stderr or "object has been modified" in stderr:
            continue
        raise RuntimeError(f"Cannot {action} observability lock on Namespace/{namespace_name}")

    raise RuntimeError(
        f"Could not {action} observability lock on Namespace/{namespace_name} "
        "after repeated resourceVersion conflicts"
    )


def claim_workload_namespace(workload_namespace: str, run_id: str) -> None:
    """Claim a shared workload namespace for one Forge run at a time."""

    _update_workload_namespace_lock(workload_namespace, run_id, claim=True)


def release_workload_namespace(workload_namespace: str, run_id: str) -> None:
    """Release the workload namespace only after the run's Receiver is removed."""

    _update_workload_namespace_lock(workload_namespace, run_id, claim=False)


def ensure_observability(settings: dict, *, workload_namespace: str, run_id: str) -> None:
    """Create this Forge run's Receiver, queues, and configured ServiceMonitors."""

    spec = _validated_settings(settings, workload_namespace=workload_namespace, run_id=run_id)
    if spec is None:
        return

    from projects.core.library import vault

    content_path = vault.get_vault_content_path(spec["vault_name"], spec["vault_content"])
    if content_path is None or not Path(content_path).is_file():
        raise FileNotFoundError("Required observability object-store vault content is unavailable")

    from projects.observability.toolbox.ensure_ingestion.main import run

    ensure_keys = (
        "run_id",
        "run_suffix",
        "cluster_label",
        "workload_namespace",
        "receiver_namespace",
        "receiver_name",
        "receiver_image",
        "receiver_storage_class",
        "receiver_pvc_size",
        "receiver_retention",
        "receiver_out_of_order_window",
        "receiver_run_as_user",
        "receiver_fs_group",
        "receiver_resources",
        "receiver_shutdown_grace_seconds",
        "queue_name",
        "namespace_regex",
        "container_names",
        "container_metric_regex",
        "forward_gpu_metrics",
        "gpu_namespaces",
        "gpu_workload_label",
        "gpu_metric_regex",
        "service_monitors",
    )
    run(
        **{key: spec[key] for key in ensure_keys},
        objstore_content_path=str(content_path),
    )


def teardown_observability(settings: dict, *, workload_namespace: str, run_id: str) -> None:
    """Remove this run's ingestion only after its final S3 upload is proven."""

    spec = _validated_settings(settings, workload_namespace=workload_namespace, run_id=run_id)
    if spec is None:
        return

    from projects.observability.toolbox.teardown_ingestion.main import run

    teardown_keys = (
        "run_id",
        "run_suffix",
        "workload_namespace",
        "receiver_namespace",
        "receiver_name",
        "queue_name",
        "service_monitors",
        "stable_polls",
        "workload_stop_timeout_seconds",
        "queue_drain_timeout_seconds",
        "queue_removal_timeout_seconds",
        "receiver_shutdown_timeout_seconds",
    )
    run(**{key: spec[key] for key in teardown_keys})
