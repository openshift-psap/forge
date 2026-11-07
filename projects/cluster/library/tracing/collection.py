"""Shared trace-collection helpers and lifecycle composition.

This module builds the collector resources (as plain dicts, so they can be
unit-tested without a cluster) and composes the capture toolbox commands. It
imports no project orchestration: callers pass explicit typed inputs.

Secret hygiene: only non-secret resources are built here. Nothing writes to an
artifact directory, and no credential is ever embedded in a manifest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projects.cluster.library.tracing.instrumentation import collector_service_name
from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    TracingSettings,
)

SPOOL_MOUNT_PATH = "/var/lib/forge-traces/spool"
SPOOL_FILENAME = "otlp.jsonl"
# Ownership labels restrict cleanup to a single capture; separate jobs and
# captures must never delete each other's resources.
OWNERSHIP_LABEL_KEY = "forge.tracing/capture-id"
COMPONENT_LABEL_KEY = "app.kubernetes.io/component"
COMPONENT_LABEL_VALUE = "forge-tracing"
MANAGED_BY_LABEL_KEY = "app.kubernetes.io/managed-by"
MANAGED_BY_LABEL_VALUE = "forge"
# A small image that surely contains a shell-less reader is not assumed; the
# spool-reader container uses a pinned busybox so `oc exec`/`oc cp` work.
DEFAULT_READER_IMAGE = "busybox:1.36.1"
SANITIZER_IMAGE = "python:3.12.8-slim-bookworm"


def sanitizer_sources() -> dict[str, str]:
    """Ship the shared policy unchanged; no duplicate policy in the sidecar."""
    library = Path(__file__).parent
    command = library.parents[1] / "toolbox" / "deploy_trace_collector" / "templates"
    return {
        "sanitizer.py": (library / "sanitizer.py").read_text(),
        "models.py": (library / "models.py").read_text(),
        "sanitize_otlp.py": (command / "sanitize_otlp.py").read_text(),
    }


class TracingResourceError(RuntimeError):
    """Raised when collector resources cannot be built or validated."""


def ownership_labels(identity: CaptureIdentity) -> dict[str, str]:
    """Return the labels that scope a capture's resources for safe cleanup."""
    return {
        OWNERSHIP_LABEL_KEY: identity.capture_id,
        COMPONENT_LABEL_KEY: COMPONENT_LABEL_VALUE,
        MANAGED_BY_LABEL_KEY: MANAGED_BY_LABEL_VALUE,
        "forge.project": identity.project,
        "forge.job_id": identity.job_id,
    }


def spool_path() -> str:
    return f"{SPOOL_MOUNT_PATH}/{SPOOL_FILENAME}"


def pvc_name(capture_id: str) -> str:
    return f"forge-trace-spool-{capture_id}"


def build_collector_resources(
    *,
    identity: CaptureIdentity,
    settings: TracingSettings,
    collector_config_text: str,
    reader_image: str = DEFAULT_READER_IMAGE,
) -> list[dict[str, Any]]:
    """Build the ConfigMap, PVC, Deployment, and Service for a capture.

    The Deployment runs the pinned collector plus a small spool-reader sidecar
    with read-only access to the PVC, so copying and checksumming do not rely on
    the collector image containing a shell, tar, or Python.
    """
    if not settings.enabled:
        raise TracingResourceError("cannot build collector resources with tracing disabled")

    namespace = identity.namespace
    name = collector_service_name(identity.capture_id)
    labels = ownership_labels(identity)
    pod_labels = dict(labels)

    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{name}-config", "namespace": namespace, "labels": labels},
        "data": {"config.yaml": collector_config_text, **sanitizer_sources()},
    }

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name(identity.capture_id),
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": settings.storage_size}},
        },
    }
    if settings.storage_class:
        pvc["spec"]["storageClassName"] = settings.storage_class

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {OWNERSHIP_LABEL_KEY: identity.capture_id}},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    # No explicit fsGroup: OpenShift assigns one from the
                    # namespace's supplemental-group range (restricted-v2 SCC),
                    # and a hardcoded value is rejected by admission. The
                    # spool-probe init container proves writability at runtime.
                    "securityContext": {"runAsNonRoot": True},
                    # The probe runs before the collector starts, with write
                    # access, so an unwritable volume fails deployment instead
                    # of being discovered only after the benchmark.
                    "initContainers": [
                        {
                            "name": "spool-probe",
                            "image": reader_image,
                            "command": [
                                "sh",
                                "-c",
                                f"touch {SPOOL_MOUNT_PATH}/.forge-probe"
                                f" && rm -f {SPOOL_MOUNT_PATH}/.forge-probe",
                            ],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"cpu": "100m", "memory": "64Mi"},
                            },
                            "volumeMounts": [{"name": "spool", "mountPath": SPOOL_MOUNT_PATH}],
                        }
                    ],
                    "containers": [
                        {
                            "name": "sanitizer",
                            "image": SANITIZER_IMAGE,
                            "command": ["python", "-B", "/opt/forge/sanitize_otlp.py"],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "readinessProbe": {
                                "exec": {
                                    "command": [
                                        "python",
                                        "-B",
                                        "-c",
                                        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4319/health', timeout=2)",
                                    ]
                                },
                                "periodSeconds": 5,
                            },
                            "volumeMounts": [
                                {"name": "sanitizer", "mountPath": "/opt/forge", "readOnly": True}
                            ],
                        },
                        {
                            "name": "collector",
                            "image": settings.collector_image,
                            "args": ["--config=/etc/otel/config.yaml"],
                            "ports": [
                                {"name": "otlp-grpc", "containerPort": 4317},
                                {"name": "otlp-http", "containerPort": 4318},
                                {"name": "health", "containerPort": 13133},
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/", "port": 13133},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 5,
                            },
                            "resources": {
                                "requests": {
                                    "cpu": settings.resource_cpu_request,
                                    "memory": settings.resource_memory_request,
                                },
                                "limits": {
                                    "cpu": settings.resource_cpu_limit,
                                    "memory": settings.resource_memory_limit,
                                },
                            },
                            "volumeMounts": [
                                {"name": "spool", "mountPath": SPOOL_MOUNT_PATH},
                                {
                                    "name": "config",
                                    "mountPath": "/etc/otel",
                                    "readOnly": True,
                                },
                            ],
                        },
                        {
                            "name": "spool-reader",
                            "image": reader_image,
                            "command": ["sleep", "infinity"],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "128Mi"},
                            },
                            "volumeMounts": [
                                {
                                    "name": "spool",
                                    "mountPath": SPOOL_MOUNT_PATH,
                                    "readOnly": True,
                                }
                            ],
                        },
                    ],
                    "volumes": [
                        {
                            "name": "sanitizer",
                            "configMap": {
                                "name": f"{name}-config",
                                "items": [
                                    {"key": "sanitize_otlp.py", "path": "sanitize_otlp.py"},
                                    {
                                        "key": "sanitizer.py",
                                        "path": "projects/cluster/library/tracing/sanitizer.py",
                                    },
                                    {
                                        "key": "models.py",
                                        "path": "projects/cluster/library/tracing/models.py",
                                    },
                                ],
                            },
                        },
                        {
                            "name": "spool",
                            "persistentVolumeClaim": {"claimName": pvc_name(identity.capture_id)},
                        },
                        {"name": "config", "configMap": {"name": f"{name}-config"}},
                    ],
                },
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "selector": {OWNERSHIP_LABEL_KEY: identity.capture_id},
            "ports": [
                {"name": "otlp-grpc", "port": 4317, "targetPort": 4317},
                {"name": "otlp-http", "port": 4318, "targetPort": 4318},
            ],
        },
    }

    return [config_map, pvc, deployment, service]


def cleanup_selector(identity: CaptureIdentity) -> str:
    """Return the label selector used to delete only this capture's resources."""
    return f"{OWNERSHIP_LABEL_KEY}={identity.capture_id}"


def load_collector_config(templates_dir) -> str:
    """Load the pinned collector configuration template beside the command."""
    from pathlib import Path

    path = Path(templates_dir) / "collector-config.yaml"
    if not path.is_file():
        raise TracingResourceError(f"collector config template not found: {path}")
    return path.read_text(encoding="utf-8")


__all__ = [
    "COMPONENT_LABEL_KEY",
    "DEFAULT_READER_IMAGE",
    "OWNERSHIP_LABEL_KEY",
    "SPOOL_FILENAME",
    "SPOOL_MOUNT_PATH",
    "TracingResourceError",
    "build_collector_resources",
    "cleanup_selector",
    "collector_service_name",
    "load_collector_config",
    "ownership_labels",
    "pvc_name",
    "spool_path",
]
