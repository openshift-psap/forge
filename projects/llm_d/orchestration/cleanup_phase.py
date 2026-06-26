from __future__ import annotations

import logging

from projects.cluster.toolbox.cleanup_operators import (
    main as cleanup_operators_command,
)
from projects.core.dsl.utils.k8s import oc_resource_exists
from projects.llm_d.toolbox.cleanup_test_resources import main as cleanup_test_resources_command

logger = logging.getLogger(__name__)


def run(*, namespace: str | None = None, cleanup_subscriptions: bool = False) -> int:
    """Delete llm_d runtime leftovers from a namespace and optionally cleanup operators."""

    cleanup_namespace(namespace=namespace)

    if cleanup_subscriptions:
        cleanup_operators()

    return 0


def cleanup_namespace(*, namespace: str | None = None) -> None:
    # Load config where it's consumed
    from projects.llm_d.orchestration import runtime_config

    if namespace is None:
        namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    inference_service_name = platform["inference_service"]["name"]
    benchmark_job_names = runtime_config.get_benchmark_job_names()
    # Multiple benchmarks may share a job_name (defaults to "guidellm-benchmark");
    # the broad labeled sweep below deletes every forge-labeled job/pod/pvc
    # regardless, so a single representative name is enough for the named pass.
    benchmark_name = benchmark_job_names[0] if benchmark_job_names else None

    if not oc_resource_exists("namespace", namespace):
        return

    cleanup_test_resources_command.run(
        namespace=namespace,
        inference_service_name=inference_service_name,
        smoke_pod_name=None,  # No specific smoke pod name for runtime cleanup
        benchmark_job_name=benchmark_name,
        cleanup_all_llm_d_resources=True,  # Enable broad cleanup for runtime
    )


def cleanup_operators(*, dry_run: bool = False) -> None:
    """Clean up operators and their associated resources based on configuration."""
    # Load config where it's consumed
    from projects.core.library import config
    from projects.llm_d.orchestration import runtime_config

    platform = runtime_config.get_platform_config()
    cleanup_config = config.project.get_config("cleanup", {})

    logger.info("Starting operator cleanup (subscriptions, CSVs, InstallPlans, OperatorGroups)")

    # Identify operators to delete based on configuration
    operators_to_delete = _identify_operators_to_delete(platform, cleanup_config)

    if not operators_to_delete:
        logger.info("No operators to clean up")
        return

    logger.info(f"Found {len(operators_to_delete)} operators to clean up")

    cleanup_operators_command.run(
        operators=operators_to_delete,
        dry_run=dry_run,
    )


def _identify_operators_to_delete(platform: dict, cleanup_config: dict) -> list[dict]:
    """Identify which operators should be cleaned up based on configuration."""

    operators = platform.get("operators", {})
    preserve_operators = cleanup_config.get("preserve_operators", {})

    logger.info(f"Found {len(operators)} operators in platform config")
    logger.info(f"Preserve configuration: {preserve_operators}")

    operators_to_delete = []

    # Handle both dict and list formats for operators
    if isinstance(operators, dict):
        operator_items = operators.items()
        logger.info("Using dict format for operators")
    else:
        operator_items = [(op.get("package"), op) for op in operators if op.get("package")]
        logger.info("Using list format for operators")

    for package_name, operator_spec in operator_items:
        logger.info(f"Processing operator: {package_name}")

        # Check if this operator should be preserved
        if preserve_operators.get(package_name, False):
            logger.info(f"Preserving operator: {package_name}")
            continue

        # Determine the namespace for the subscription
        if isinstance(operator_spec, dict):
            namespace = operator_spec.get("namespace", "openshift-operators")
        else:
            namespace = "openshift-operators"

        logger.info(f"Adding to deletion list: {package_name} in namespace {namespace}")
        operators_to_delete.append({"name": package_name, "namespace": namespace})

    logger.info(f"Identified {len(operators_to_delete)} operators for deletion")
    return operators_to_delete
