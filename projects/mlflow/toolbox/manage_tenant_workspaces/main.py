"""Create and restore MLflow workspace namespaces used by benchmark tenants."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils import write_json
from projects.core.dsl.utils.k8s import oc, oc_get_json

WORKSPACE_LABEL = "mlflow-enabled"
WORKSPACE_ANNOTATION = "mlflow.kubeflow.org/workspace-description"
WORKSPACE_DESCRIPTION = "MLflow performance testing tenant workspace"
K6_SERVICE_ACCOUNT = "default"
MLFLOW_INTEGRATION_ROLE = "mlflow-operator-mlflow-integration"
TENANT_ROLE_BINDING = "mlflow-perf-testing-access"
NAMESPACE_DELETE_TIMEOUT_SECONDS = 300

logger = logging.getLogger(__name__)


@dataclass
class NamespaceState:
    """Namespace metadata and RBAC changes owned by this test run."""

    name: str
    uid: str
    created: bool
    previous_workspace_label: str | None
    previous_workspace_description: str | None
    role_binding_uid: str | None = None


@entrypoint
def run(
    *,
    tenant_count: int = 0,
    k6_namespace: str | None = None,
    cleanup_from: str | Path | None = None,
) -> Path | None:
    """Provision MLflow tenant workspaces or restore them from saved state.

    Args:
        tenant_count: Number of benchmark workspaces, named tenant-1 through tenant-N
        k6_namespace: Namespace of the k6 service account to grant access
        cleanup_from: Workspace state file emitted by the provisioning command
    """
    if cleanup_from is not None:
        cleanup_from = str(Path(cleanup_from).resolve())
    return getattr(execute_tasks(locals()), "state_path", None)


@task
def manage_tenant_workspaces(args, ctx):
    """Apply tenant workspace changes or undo changes recorded in a state file."""
    if args.cleanup_from is not None:
        if args.tenant_count or args.k6_namespace is not None:
            raise ValueError("cleanup_from cannot be combined with tenant_count or k6_namespace")
        state_file = Path(args.cleanup_from)
        states = [
            NamespaceState(**item) for item in json.loads(state_file.read_text(encoding="utf-8"))
        ]
        if [state.name for state in states] != workspaces(len(states)):
            raise ValueError("cleanup state does not contain the benchmark's tenant workspaces")
        _cleanup(states)
        return f"Restored {len(states)} tenant workspaces from {state_file}"

    if args.tenant_count <= 0 or not args.k6_namespace:
        raise ValueError("provisioning requires a positive tenant_count and a k6_namespace")

    states: list[NamespaceState] = []
    state_path = args.artifact_dir / "workspaces.json"
    try:
        _ensure(states, tenant_count=args.tenant_count, k6_namespace=args.k6_namespace)
        write_json(state_path, [asdict(state) for state in states])
    except Exception as error:
        try:
            _cleanup(states)
        except Exception as rollback_error:
            raise ExceptionGroup(
                "Tenant workspace provisioning and rollback failed", [error, rollback_error]
            ) from None
        raise

    ctx.state_path = state_path
    return f"Saved workspace state in {state_path}"


def _ensure(states: list[NamespaceState], *, tenant_count: int, k6_namespace: str) -> None:
    """Make each tenant namespace a discoverable, authorized MLflow workspace."""
    for namespace in workspaces(tenant_count):
        resource = oc_get_json("namespace", name=namespace, ignore_not_found=True)
        if resource is not None and resource["metadata"].get("deletionTimestamp"):
            logger.info("Waiting for terminating workspace namespace %s", namespace)
            _delete_namespace_and_wait(namespace)
            resource = None
        created = resource is None
        if created:
            result = oc("create", "namespace", namespace, "-o", "json", log_stdout=False)
            resource = json.loads(result.stdout)
        metadata = resource["metadata"]

        state = NamespaceState(
            name=namespace,
            uid=metadata["uid"],
            created=created,
            previous_workspace_label=metadata.get("labels", {}).get(WORKSPACE_LABEL),
            previous_workspace_description=metadata.get("annotations", {}).get(
                WORKSPACE_ANNOTATION
            ),
        )
        states.append(state)
        oc("label", "namespace", namespace, f"{WORKSPACE_LABEL}=true", "--overwrite")
        oc(
            "annotate",
            "namespace",
            namespace,
            f"{WORKSPACE_ANNOTATION}={WORKSPACE_DESCRIPTION}",
            "--overwrite",
        )
        _ensure_role_binding(state, k6_namespace=k6_namespace)


def _cleanup(states: list[NamespaceState]) -> None:
    """Delete test-created namespaces and restore metadata on reused ones."""
    errors: list[Exception] = []
    for state in reversed(states):
        try:
            _cleanup_workspace(state)
        except Exception as error:
            error.add_note(f"While restoring workspace {state.name}")
            errors.append(error)
    if errors:
        raise ExceptionGroup("Failed to restore one or more tenant workspaces", errors)


def _cleanup_workspace(state: NamespaceState) -> None:
    resource = oc_get_json("namespace", name=state.name, ignore_not_found=True)
    if resource is None or resource["metadata"]["uid"] != state.uid:
        return
    if state.created:
        _delete_namespace_and_wait(state.name)
        return

    if state.role_binding_uid is not None:
        binding = oc_get_json(
            "rolebinding",
            name=TENANT_ROLE_BINDING,
            namespace=state.name,
            ignore_not_found=True,
        )
        if binding is not None and binding["metadata"]["uid"] == state.role_binding_uid:
            oc(
                "delete",
                "rolebinding",
                TENANT_ROLE_BINDING,
                "-n",
                state.name,
                "--ignore-not-found=true",
            )

    metadata = resource["metadata"]
    for action, key, current, applied, previous in (
        (
            "label",
            WORKSPACE_LABEL,
            metadata.get("labels", {}).get(WORKSPACE_LABEL),
            "true",
            state.previous_workspace_label,
        ),
        (
            "annotate",
            WORKSPACE_ANNOTATION,
            metadata.get("annotations", {}).get(WORKSPACE_ANNOTATION),
            WORKSPACE_DESCRIPTION,
            state.previous_workspace_description,
        ),
    ):
        if current == applied and current != previous:
            update = f"{key}-" if previous is None else f"{key}={previous}"
            oc(action, "namespace", state.name, update, "--overwrite")


def workspaces(tenant_count: int) -> list[str]:
    """Return the workspace names used by the read and write workloads."""
    return [f"tenant-{tenant_id}" for tenant_id in range(1, tenant_count + 1)]


def _delete_namespace_and_wait(namespace: str) -> None:
    """Delete a test-created workspace namespace and wait until it is gone."""
    timeout = NAMESPACE_DELETE_TIMEOUT_SECONDS
    oc(
        "delete",
        "namespace",
        namespace,
        "--ignore-not-found=true",
        "--wait=true",
        f"--timeout={timeout}s",
        timeout_seconds=timeout + 30,
    )


def _ensure_role_binding(state: NamespaceState, *, k6_namespace: str) -> None:
    """Grant the K6 service account MLflow access scoped to this workspace."""
    binding = oc_get_json(
        "rolebinding",
        name=TENANT_ROLE_BINDING,
        namespace=state.name,
        ignore_not_found=True,
    )
    if binding is not None:
        expected_subject = {
            "kind": "ServiceAccount",
            "name": K6_SERVICE_ACCOUNT,
            "namespace": k6_namespace,
        }
        role_ref = binding.get("roleRef", {})
        if (
            role_ref.get("apiGroup") != "rbac.authorization.k8s.io"
            or role_ref.get("kind") != "ClusterRole"
            or role_ref.get("name") != MLFLOW_INTEGRATION_ROLE
            or expected_subject not in binding.get("subjects", [])
        ):
            raise RuntimeError(
                f"RoleBinding {TENANT_ROLE_BINDING!r} in workspace {state.name!r} "
                "exists but does not grant the expected MLflow integration role"
            )
        return

    manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": TENANT_ROLE_BINDING, "namespace": state.name},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": MLFLOW_INTEGRATION_ROLE,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": K6_SERVICE_ACCOUNT,
                "namespace": k6_namespace,
            }
        ],
    }
    result = oc(
        "create",
        "-f",
        "-",
        "-o",
        "json",
        input_text=json.dumps(manifest),
        log_stdout=False,
    )
    state.role_binding_uid = json.loads(result.stdout)["metadata"]["uid"]


if __name__ == "__main__":
    run.main()
