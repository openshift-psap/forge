import logging
import os

from projects.core.dsl.utils.k8s import oc
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)
FOURNOS_JOB_LABEL = "fournos.dev/job-name"
JOB_RESOURCE_TYPES = (
    "inferenceservice",
    "servingruntime",
    "job",
    "pod",
    "pvc",
    "deployment",
    "replicaset",
    "service",
    "secret",
)


def prepare():
    ns = runtime_config.get_namespace()
    deploy_cfg = runtime_config.get_deploy_config()
    logger.info(f"Preparing namespace {ns} for rhaiis benchmarks")

    result = oc("whoami", check=False)
    if result.returncode != 0:
        raise RuntimeError("Cannot connect to cluster")
    logger.info(f"Connected to cluster as {result.stdout.strip()}")

    result = oc("get", "namespace", ns, check=False)
    if result.returncode != 0:
        oc("create", "namespace", ns)
        logger.info(f"Created namespace {ns}")
    else:
        logger.info(f"Namespace {ns} already exists")

    sa_name = deploy_cfg.get("service_account_name", "")
    if sa_name:
        result = oc("get", "serviceaccount", sa_name, "-n", ns, check=False)
        if result.returncode != 0:
            oc("create", "serviceaccount", sa_name, "-n", ns)
            logger.info(f"Created service account {sa_name}")
        else:
            logger.info(f"Service account {sa_name} already exists")

    secret_name = deploy_cfg.get("image_pull_secret", "")
    if secret_name:
        result = oc("get", "secret", secret_name, "-n", ns, check=False)
        if result.returncode == 0:
            logger.info(f"Image pull secret {secret_name} exists")
        else:
            logger.warning(
                f"Image pull secret {secret_name} not found in {ns} — "
                "deployment may fail if images require authentication"
            )


def _job_resources(resource_type: str, ns: str, fjob: str) -> list[str]:
    """Find only resources explicitly labelled for this FournosJob."""
    selector = f"{FOURNOS_JOB_LABEL}={fjob}"
    result = oc("get", resource_type, "-n", ns, "-l", selector, "-o", "name", log_stdout=False)
    return sorted(name for name in result.stdout.splitlines() if name)


def _delete_job_resources(ns: str, fjob: str) -> None:
    for resource_type in JOB_RESOURCE_TYPES:
        for name in _job_resources(resource_type, ns, fjob):
            oc(
                "delete",
                name,
                "-n",
                ns,
                "--ignore-not-found",
                "--cascade=foreground",
                "--wait=true",
                "--timeout=240s",
                log_stdout=False,
            )

    remaining = {
        resource_type: len(resources)
        for resource_type in JOB_RESOURCE_TYPES
        if (resources := _job_resources(resource_type, ns, fjob))
    }
    if remaining:
        raise RuntimeError(f"RHAIIS cleanup incomplete in {ns}: {remaining}")


def cleanup():
    ns = runtime_config.get_namespace()
    logger.info(f"Cleaning up rhaiis benchmark resources in {ns}")

    fjob = os.environ.get("FJOB_NAME", "")
    if fjob:
        _delete_job_resources(ns, fjob)
    else:
        oc("delete", "inferenceservice", "--all", "-n", ns, "--ignore-not-found")
        oc("delete", "servingruntime", "--all", "-n", ns, "--ignore-not-found")
        oc("delete", "job", "--all", "-n", ns, "--ignore-not-found")
        oc("delete", "pod", "--all", "-n", ns, "--ignore-not-found")

    logger.info("Cleanup complete")
