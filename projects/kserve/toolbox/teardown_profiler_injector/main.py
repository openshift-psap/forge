#!/usr/bin/env python3
"""Tear down the vLLM profiler env-injector deployed by deploy_profiler_injector.

Removes only resources carrying the ownership labels the deploy applied:
the cluster-scoped MutatingWebhookConfiguration (removed first, so pod
creation cluster-wide stops hitting the webhook immediately), the injector
namespace (cascading the deployment, service, service account and TLS
secret), and the per-namespace ConfigMap. Resources deployed by someone
else — reused by an idempotent deploy but never labelled — are detected
and skipped, never deleted.
"""

from __future__ import annotations

import json

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)
from projects.kserve.toolbox.deploy_profiler_injector.main import (
    CERT_SECRET_NAME,
    CONFIGMAP_NAME,
    webhook_name,
)

DEFAULT_INJECTOR_NAMESPACE = "vllm-profiler"


@entrypoint
def run(
    *,
    namespace: str,
    injector_namespace: str = DEFAULT_INJECTOR_NAMESPACE,
    labels: dict[str, str] | None = None,
):
    """Tear down the forge-owned vLLM profiler env-injector.

    Args:
        namespace: Target namespace of the profiled pods (the ConfigMap to
            remove lives here).
        injector_namespace: Namespace hosting the webhook deployment.
        labels: Ownership labels used by deploy_profiler_injector. Only
            resources carrying all of them are removed; required, so a
            teardown can never delete an injector deployed by someone else.
    """
    if not labels:
        raise ValueError(
            "teardown_profiler_injector requires ownership labels; "
            "pass the same labels used by deploy_profiler_injector"
        )
    return execute_tasks(locals())


def _is_owned(resource: str, name: str, namespace: str | None, labels: dict[str, str]) -> str:
    """Return 'absent', 'owned', or 'foreign' for a resource.

    A resource is owned when it carries every ownership label with a
    matching value. Unlabelled or differently-labelled resources — e.g. an
    injector deployed manually, which an idempotent deploy reuses without
    labelling — are foreign and must never be deleted.
    """
    ns_args = f"-n {namespace}" if namespace else ""
    result = shell.run(
        f"oc get {resource} {name} {ns_args} -o jsonpath='{{.metadata.labels}}'",
        check=False,
    )
    if result.returncode != 0:
        return "absent"

    try:
        resource_labels = json.loads(result.stdout.strip().strip("'") or "{}")
    except ValueError:
        resource_labels = {}

    if not isinstance(resource_labels, dict):
        resource_labels = {}

    if all(resource_labels.get(key) == value for key, value in labels.items()):
        return "owned"
    return "foreign"


def _remove(resource: str, name: str, ownership: str, namespace: str | None = None) -> str:
    """Delete an owned resource, or report why it is kept."""
    if ownership == "absent":
        return f"{resource} {name} already gone"
    if ownership == "foreign":
        return f"{resource} {name} not owned by this deployment, skipping"

    ns_args = f"-n {namespace}" if namespace else ""
    shell.run(f"oc delete {resource} {name} {ns_args} --ignore-not-found")
    return f"Deleted {resource} {name}"


@task
def remove_webhook_configuration(args, context):
    webhook = webhook_name(args.injector_namespace)
    ownership = _is_owned("mutatingwebhookconfiguration", webhook, None, args.labels)
    return _remove("mutatingwebhookconfiguration", webhook, ownership)


@task
def remove_injector_namespace(args, context):
    """Delete the injector namespace (cascades deployment, service, SA, secret)."""
    ownership = _is_owned("namespace", args.injector_namespace, None, args.labels)
    return _remove("namespace", args.injector_namespace, ownership)


@task
def remove_profiler_configmap(args, context):
    ownership = _is_owned("configmap", CONFIGMAP_NAME, args.namespace, args.labels)
    return _remove("configmap", CONFIGMAP_NAME, ownership, namespace=args.namespace)


@task
def remove_orphaned_tls_secret(args, context):
    """Remove the TLS secret when the namespace deletion was skipped.

    The secret is cascaded by the namespace deletion; this only matters
    when the namespace itself was foreign (e.g. pre-existing) but the
    secret inside it is ours.
    """
    namespace_ownership = _is_owned("namespace", args.injector_namespace, None, args.labels)
    if namespace_ownership == "owned":
        return "Secret cascaded by the namespace deletion"

    ownership = _is_owned("secret", CERT_SECRET_NAME, args.injector_namespace, args.labels)
    return _remove("secret", CERT_SECRET_NAME, ownership, namespace=args.injector_namespace)
