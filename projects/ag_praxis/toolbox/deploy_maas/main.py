#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import yaml

from projects.core.dsl import EarlyReturn, always, entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc, oc, oc_exec, oc_get_json, oc_resource_exists

logger = logging.getLogger("DSL")

DEPLOYER_IMAGE = "registry.redhat.io/openshift4/ose-cli:latest"
POD_NAME = "maas-deployer"
SA_NAME = "maas-deployer"
CRB_NAME = "maas-deployer-cluster-admin"
CLONE_DIR = "/tmp/maas"
KUBECONFIG_POD_PATH = "/tmp/kubeconfig"


@entrypoint
def run(
    *,
    namespace: str,
    repo_url: str,
    repo_ref: str,
    operator_type: str,
    keep_pod: bool = True,
    skip_conflicting_ns_check: bool = True,
    verbose: bool = True,
    force_reinstall: bool = False,
):
    """Deploy MaaS by cloning the upstream repo and running deploy.sh inside a Pod.

    Args:
        namespace: Namespace for the deployer Pod and ServiceAccount.
        repo_url: URL of the models-as-a-service git repository.
        repo_ref: Git branch, tag, or SHA to checkout.
        operator_type: Operator type passed to deploy.sh (e.g. 'odh').
        keep_pod: If True, keep the Pod after deployment and reuse it if it already exists.
        skip_conflicting_ns_check: If True, skip the check for conflicting namespaces.
        verbose: If True, show command output live instead of redirecting to log files.
        force_reinstall: If True, reinstall MaaS even if already available
    """
    return execute_tasks(locals())


CONFLICTING_NAMESPACES = ["opendatahub", "kuadrant-system", "ai-tenants", "opendatahub-monitoring"]
MAAS_API_NAMESPACE = "odh-ai-gateway-infra"


@task
def check_maas_already_deployed(args, ctx):
    """Check if MaaS API is already deployed and healthy."""
    if not oc_resource_exists("deployment", "maas-api", namespace=MAAS_API_NAMESPACE):
        return "maas-api deployment not found, proceeding with installation"

    deploy = oc_get_json("deployment", name="maas-api", namespace=MAAS_API_NAMESPACE)
    conditions = {c["type"]: c["status"] for c in deploy.get("status", {}).get("conditions", [])}

    if conditions.get("Available") != "True":
        raise RuntimeError(
            f"maas-api deployment exists but is not healthy: conditions={conditions}"
        )

    if args.force_reinstall:
        return "maas-api already deployed and health, but --force-reinstall flag is sest"

    return EarlyReturn("maas-api is already deployed and healthy, skipping installation")


@task
def check_no_conflicting_namespaces(args, ctx):
    """Verify no conflicting namespaces exist before deployment."""
    if args.skip_conflicting_ns_check:
        return "Skipped conflicting namespace check"

    existing = [ns for ns in CONFLICTING_NAMESPACES if oc_resource_exists("namespace", ns)]
    if existing:
        raise RuntimeError(
            f"Conflicting namespaces must not exist before deploying MaaS: {', '.join(existing)}"
        )
    return "No conflicting namespaces found"


@task
def remove_aitenant_bootstrap_annotation(args, ctx):
    """Remove the default AITenant bootstrap annotation from the MaaS ConfigMap."""
    if not oc_resource_exists("config.maas.opendatahub.io", "default"):
        return "ConfigMap maas.opendatahub.io/default not found, nothing to do"

    oc(
        "annotate",
        "config.maas.opendatahub.io",
        "default",
        "maas.opendatahub.io/default-aitenant-bootstrapped-",
    )
    return "Removed default-aitenant-bootstrapped annotation"


@task
def ensure_namespace(args, ctx):
    """Ensure the deployer namespace exists."""
    if oc_resource_exists("namespace", args.namespace):
        return f"Namespace {args.namespace} exists"
    oc("create", "namespace", args.namespace)
    return f"Created namespace {args.namespace}"


@task
def create_service_account(args, ctx):
    """Create the deployer ServiceAccount and cluster-admin binding."""
    if not oc_resource_exists("serviceaccount", SA_NAME, namespace=args.namespace):
        oc("create", "serviceaccount", SA_NAME, "-n", args.namespace)

    if not oc_resource_exists("clusterrolebinding", CRB_NAME):
        oc(
            "create",
            "clusterrolebinding",
            CRB_NAME,
            "--clusterrole=cluster-admin",
            f"--serviceaccount={args.namespace}:{SA_NAME}",
        )

    return f"ServiceAccount {SA_NAME} with cluster-admin binding ready"


@task
def generate_kubeconfig(args, ctx):
    """Generate a temporary kubeconfig using the deployer SA token."""
    fd, kubeconfig_file = tempfile.mkstemp(prefix="maas-deployer-", suffix="-kubeconfig")
    os.close(fd)
    ctx.kubeconfig_path = Path(kubeconfig_file)

    api_server = oc("whoami", "--show-server", log_stdout=True).stdout.strip()

    token_result = oc(
        "create",
        "token",
        SA_NAME,
        "-n",
        args.namespace,
        "--duration=4h",
        handled_secretly=True,
    )
    token = token_result.stdout.strip()

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "cluster": {
                    "server": api_server,
                    "insecure-skip-tls-verify": True,
                },
                "name": "cluster",
            }
        ],
        "contexts": [
            {
                "context": {
                    "cluster": "cluster",
                    "namespace": args.namespace,
                    "user": SA_NAME,
                },
                "name": SA_NAME,
            }
        ],
        "current-context": SA_NAME,
        "users": [{"name": SA_NAME, "user": {"token": token}}],
    }

    with open(ctx.kubeconfig_path, "w") as f:
        yaml.dump(kubeconfig, f, default_flow_style=False)

    os.chmod(ctx.kubeconfig_path, 0o600)

    return f"Generated kubeconfig at {ctx.kubeconfig_path}"


@task
def create_deployer_pod(args, ctx):
    """Create or reuse the deployer Pod."""
    ctx.pod_reused = False

    if args.keep_pod and oc_resource_exists("pod", POD_NAME, namespace=args.namespace):
        result = oc(
            "get",
            "pod",
            POD_NAME,
            "-n",
            args.namespace,
            "-o",
            "jsonpath={.status.phase}",
            log_stdout=True,
        )
        if result.stdout.strip() == "Running":
            ctx.pod_reused = True
            return f"Reusing existing pod {POD_NAME}"

    best_effort_oc("delete", "pod", POD_NAME, "-n", args.namespace, "--ignore-not-found")

    oc(
        "run",
        POD_NAME,
        f"--image={DEPLOYER_IMAGE}",
        "-n",
        args.namespace,
        "--restart=Never",
        "--command",
        "--",
        "sleep",
        "inf",
    )

    oc(
        "wait",
        "pod",
        POD_NAME,
        "-n",
        args.namespace,
        "--for=condition=Ready",
        "--timeout=120s",
    )

    return f"Pod {POD_NAME} is ready"


@task
def copy_kubeconfig_to_pod(args, ctx):
    """Copy the generated kubeconfig into the deployer Pod."""
    oc("cp", str(ctx.kubeconfig_path), f"{args.namespace}/{POD_NAME}:{KUBECONFIG_POD_PATH}")
    ctx.kubeconfig_path.unlink(missing_ok=True)
    return f"Copied kubeconfig to {POD_NAME}:{KUBECONFIG_POD_PATH}"


@task
def install_dependencies(args, ctx):
    """Install git, jq, gettext, and kustomize in the deployer Pod."""
    if ctx.pod_reused:
        return "Skipped — reusing existing pod"

    dnf_log = None
    kustomize_log = None
    if not args.verbose:
        logs_dir = args.artifact_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        dnf_log = logs_dir / "dnf-install.log"
        kustomize_log = logs_dir / "kustomize-install.log"

    oc_exec(
        "sh",
        "-c",
        "dnf install -y git jq gettext",
        namespace=args.namespace,
        pod=POD_NAME,
        timeout_seconds=300,
        stdout_dest=dnf_log,
    )
    kustomize_install = (
        "curl -sSf https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh"
        " -o /tmp/install_kustomize.sh"
        " && bash /tmp/install_kustomize.sh /usr/local/bin"
    )
    oc_exec(
        "sh",
        "-c",
        kustomize_install,
        namespace=args.namespace,
        pod=POD_NAME,
        timeout_seconds=120,
        stdout_dest=kustomize_log,
    )
    return "Installed git, jq, gettext, and kustomize in deployer pod"


@task
def clone_repository(args, ctx):
    """Clone the MaaS repository into the deployer Pod."""
    clone_cmd = f"rm -rf {CLONE_DIR} && git clone --branch {args.repo_ref} --single-branch {args.repo_url} {CLONE_DIR}"
    oc_exec(
        "sh",
        "-c",
        clone_cmd,
        namespace=args.namespace,
        pod=POD_NAME,
        timeout_seconds=300,
    )
    return f"Cloned {args.repo_url}@{args.repo_ref} to {CLONE_DIR}"


@task
def run_deploy_script(args, ctx):
    """Run deploy.sh inside the deployer Pod."""
    deploy_cmd = (
        f"cd {CLONE_DIR} && "
        f"KUBECONFIG={KUBECONFIG_POD_PATH} "
        f"./scripts/deploy.sh --operator-type {args.operator_type}"
    )
    oc_exec(
        "sh",
        "-c",
        deploy_cmd,
        namespace=args.namespace,
        pod=POD_NAME,
        timeout_seconds=1800,
    )
    return "deploy.sh completed"


@always
@task
def cleanup_kubeconfig(args, ctx):
    """Delete the local temporary kubeconfig file."""
    kubeconfig_path = getattr(ctx, "kubeconfig_path", None)
    if not kubeconfig_path:
        return "No kubeconfig to clean up"
    try:
        Path(kubeconfig_path).unlink(missing_ok=True)
    except OSError:
        pass
    return f"Deleted local kubeconfig {kubeconfig_path}"


@always
@task
def cleanup_deployer_pod(args, ctx):
    """Delete the deployer Pod unless keep_pod is set."""
    if args.keep_pod:
        return "Keeping deployer pod (keep_pod=True)"
    best_effort_oc("delete", "pod", POD_NAME, "-n", args.namespace, "--ignore-not-found")
    return "Cleaned up deployer pod"


if __name__ == "__main__":
    run.main()
