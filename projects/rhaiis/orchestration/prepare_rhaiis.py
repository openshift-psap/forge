import logging
import os

from projects.core.dsl.utils.k8s import oc
from projects.core.library import config
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


def _observability_context(ns: str) -> tuple[dict, str] | None:
    settings = config.project.get_config("observability", {}, print=False, warn=False)
    if not settings.get("enabled", False):
        return None

    from projects.observability.library.runtime import (
        resolve_run_id,
        validate_observability_run,
    )

    run_id = resolve_run_id(settings)
    validate_observability_run(settings, workload_namespace=ns, run_id=run_id)
    model = runtime_config.get_model(runtime_config.get_test_model_key())
    runtime_config.derive_run_deployment_name(model["hf_model_id"], ns)
    return settings, run_id


def _ensure_workload_namespace(ns: str) -> None:
    result = oc("get", "namespace", ns, check=False)
    if result.returncode == 0:
        logger.info("Namespace %s already exists", ns)
        return

    result = oc("create", "namespace", ns, check=False)
    if result.returncode != 0 and "alreadyexists" not in result.stderr.lower():
        raise RuntimeError(f"Could not create workload namespace {ns}")
    logger.info("Created namespace %s", ns)


def _assert_namespace_available(ns: str, run_id: str) -> bool:
    from projects.observability.library.runtime import get_workload_namespace_owner

    owner = get_workload_namespace_owner(ns)
    if owner is not None and owner != run_id:
        raise RuntimeError(f"Namespace {ns} is currently owned by another observability run")
    return owner == run_id


def prepare():
    ns = runtime_config.get_namespace()
    observability_context = _observability_context(ns)
    deploy_cfg = runtime_config.get_deploy_config()
    logger.info(f"Preparing namespace {ns} for rhaiis benchmarks")

    result = oc("whoami", check=False)
    if result.returncode != 0:
        raise RuntimeError("Cannot connect to cluster")
    logger.info(f"Connected to cluster as {result.stdout.strip()}")

    if observability_context:
        from projects.observability.library.runtime import claim_workload_namespace

        _, run_id = observability_context
        _ensure_workload_namespace(ns)
        claim_workload_namespace(ns, run_id)
    else:
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

    if observability_context:
        from projects.observability.library.runtime import ensure_observability

        observability_settings, run_id = observability_context
        ensure_observability(observability_settings, workload_namespace=ns, run_id=run_id)


def _delete_resources_by_suffix(resource_type: str, ns: str, suffix: str) -> None:
    """Delete resources whose names end with the FJOB suffix."""
    result = oc("get", resource_type, "-n", ns, "-o", "name", check=False, log_stdout=False)
    if result.returncode != 0 or not result.stdout:
        return
    for line in result.stdout.strip().splitlines():
        name = line.strip()
        if name.endswith(f"-{suffix}"):
            oc("delete", name, "-n", ns, "--ignore-not-found", check=False)


def _cleanup_benchmark_resources(ns: str) -> None:
    fjob = os.environ.get("FJOB_NAME", "")
    if fjob:
        suffix = fjob.rsplit("-", 1)[-1]
        for resource_type in ("inferenceservice", "servingruntime", "job", "pod", "pvc"):
            _delete_resources_by_suffix(resource_type, ns, suffix)
    else:
        oc("delete", "inferenceservice", "--all", "-n", ns, "--ignore-not-found", check=False)
        oc("delete", "servingruntime", "--all", "-n", ns, "--ignore-not-found", check=False)
        oc("delete", "job", "--all", "-n", ns, "--ignore-not-found", check=False)
        oc("delete", "pod", "--all", "-n", ns, "--ignore-not-found", check=False)


def _cleanup_run_scoped_resources(ns: str) -> None:
    """Stop RHAIIS inference and GuideLLM producers in its owned namespace."""
    model_key = runtime_config.get_test_model_key()
    model = runtime_config.get_model(model_key)
    deployment_name = runtime_config.derive_run_deployment_name(model["hf_model_id"], ns)
    for resource_type in ("inferenceservice", "servingruntime"):
        oc("delete", resource_type, deployment_name, "-n", ns, "--ignore-not-found")

    workload_keys = config.project.get_config("tests.rhaiis.workload_keys", []) or [
        runtime_config.get_test_workload_key()
    ]
    for workload_key in set(workload_keys):
        for prefix in ("guidellm-warmup", "guidellm-bench", "guidellm-profiler"):
            job_name = runtime_config.derive_guidellm_job_name(
                prefix, workload_key, deployment_name
            )
            oc("delete", "job", job_name, "-n", ns, "--ignore-not-found")


def cleanup():
    ns = runtime_config.get_namespace()
    observability_context = _observability_context(ns)
    logger.info(f"Cleaning up rhaiis benchmark resources in {ns}")

    if observability_context:
        from projects.observability.library.runtime import (
            release_workload_namespace,
            teardown_observability,
        )

        observability_settings, run_id = observability_context
        namespace_claimed = _assert_namespace_available(ns, run_id)
        _cleanup_run_scoped_resources(ns)
        teardown_observability(observability_settings, workload_namespace=ns, run_id=run_id)
        if namespace_claimed:
            release_workload_namespace(ns, run_id)
    else:
        _cleanup_benchmark_resources(ns)
    logger.info("Cleanup complete")
