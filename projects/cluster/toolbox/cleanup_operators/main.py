#!/usr/bin/env python3

from __future__ import annotations

import logging
from collections import deque

import yaml

from projects.core.dsl import entrypoint, execute_tasks, shell, task
from projects.core.dsl.utils.k8s import oc_resource_exists

logger = logging.getLogger(__name__)


@entrypoint
def run(
    operators: list[dict],
    *,
    dry_run: bool = False,
) -> int:
    """
    Clean up specified operators and all their associated resources.

    This script removes:
    - Subscriptions
    - Associated CSVs (ClusterServiceVersions)
    - Associated InstallPlans
    - OperatorGroups (only if no other operators remain in the namespace)

    Args:
        operators: List of dicts with 'name' and 'namespace' keys
        dry_run: If True, only show what would be deleted without actually deleting
    """

    execute_tasks(locals())
    return 0


@task
def setup_directories(args, ctx):
    """Create artifact directories"""

    shell.mkdir("artifacts")
    return "Prepared directories for operator cleanup"


@task
def discover_related_operators(args, ctx):
    """Discover related operators from OLM InstallPlan ownerReferences"""
    requested_operators = [(op["name"], op["namespace"]) for op in args.operators]
    ctx.all_operators = _expand_operators_with_installplan_owners(requested_operators)

    return f"Discovered {len(ctx.all_operators)} operator subscription(s) to clean up"


@task
def validate_operators(args, ctx):
    """Categorize operators by subscription existence"""

    existing_subscriptions = []
    missing_subscriptions = []

    for operator_name, namespace in ctx.all_operators:
        if oc_resource_exists("subscription", operator_name, namespace=namespace):
            existing_subscriptions.append((operator_name, namespace))
        else:
            missing_subscriptions.append((operator_name, namespace))

    ctx.existing_subscriptions = existing_subscriptions
    ctx.missing_subscriptions = missing_subscriptions

    return f"Found {len(existing_subscriptions)} existing subscriptions, {len(missing_subscriptions)} missing (will clean up resources for all)"


def _expand_operators_with_installplan_owners(
    operators: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    expanded_operators = []
    seen_operators = set(operators)
    pending_operators = deque(operators)
    installplans_by_namespace: dict[str, dict] = {}

    while pending_operators:
        operator_name, namespace = pending_operators.popleft()
        expanded_operators.append((operator_name, namespace))

        # Follow ownerReferences transitively because dependency InstallPlans can
        # point back to the root subscription or to other related subscriptions.
        if namespace not in installplans_by_namespace:
            installplans_by_namespace[namespace] = _fetch_installplans(namespace)

        owner_names = _subscription_owner_refs_from_installplans(
            installplans_by_namespace[namespace],
            operator_name,
        )
        for owner_name in owner_names:
            owner_key = (owner_name, namespace)
            if owner_key in seen_operators:
                continue

            seen_operators.add(owner_key)
            logger.info(
                f"Discovered related subscription from InstallPlan ownerReferences: "
                f"{owner_name} in namespace {namespace}"
            )
            pending_operators.append(owner_key)

    return expanded_operators


def _fetch_installplans(namespace: str) -> dict:
    result = shell.run(
        f"oc get installplan -n {namespace} -o yaml",
        check=False,
        log_stdout=False,
    )

    if result.returncode != 0:
        logger.warning(
            f"Failed to list InstallPlans in namespace {namespace} "
            f"(exit {result.returncode}); related subscriptions may not be fully discovered: "
            f"{result.stderr}"
        )
        return {}

    try:
        return yaml.safe_load(result.stdout) or {}
    except yaml.YAMLError as e:
        logger.warning(f"Could not parse InstallPlans in namespace {namespace}: {e}")
        return {}


def _subscription_owner_refs_from_installplans(
    installplans: dict,
    subscription_name: str,
) -> list[str]:
    owner_names = []
    seen_owner_names = set()
    for installplan in installplans.get("items", []):
        owner_references = installplan.get("metadata", {}).get("ownerReferences", [])
        subscription_owner_names = dict.fromkeys(
            owner_reference.get("name")
            for owner_reference in owner_references
            if owner_reference.get("kind") == "Subscription" and owner_reference.get("name")
        )
        if subscription_name not in subscription_owner_names:
            continue

        for owner_name in subscription_owner_names:
            if owner_name == subscription_name:
                continue
            if owner_name in seen_owner_names:
                continue

            seen_owner_names.add(owner_name)
            owner_names.append(owner_name)

    return owner_names


@task
def cleanup_operators(args, ctx):
    """Delete operators and all associated resources"""

    if not ctx.all_operators:
        return "No operators to clean up"

    cleaned_count = 0
    for operator_name, namespace in ctx.all_operators:
        if args.dry_run:
            logger.info(
                f"[DRY RUN] Would clean up operator {operator_name} in namespace {namespace}"
            )
        else:
            logger.info(f"Cleaning up operator {operator_name} in namespace {namespace}")

            # First capture associated resources before deletion
            _capture_associated_resources(operator_name, namespace, args.artifact_dir)

            # Delete subscription if it exists
            if (operator_name, namespace) in ctx.existing_subscriptions:
                shell.run(
                    f"oc delete subscription {operator_name} -n {namespace} --ignore-not-found=true",
                    check=False,
                )

            # Clean up associated resources (always, even if subscription doesn't exist)
            _cleanup_associated_resources(operator_name, namespace)
            cleaned_count += 1

    if args.dry_run:
        return f"[DRY RUN] Would clean up {len(ctx.all_operators)} operators and all associated resources"
    else:
        return f"Cleaned up {cleaned_count} operators and all associated resources"


def _capture_associated_resources(subscription_name, namespace, artifact_dir):
    """Capture CSVs, InstallPlans, and OperatorGroups before deletion"""

    # Capture CSVs owned by this subscription
    shell.run(
        f"oc get csv -n {namespace} "
        f"-l operators.coreos.com/{subscription_name}.{namespace} "
        "-o yaml",
        stdout_dest=artifact_dir / "artifacts" / f"{subscription_name}-csvs.yaml",
        check=False,
        log_stdout=False,
    )

    # Capture InstallPlans
    shell.run(
        f"oc get installplan -n {namespace} "
        f"-l operators.coreos.com/{subscription_name}.{namespace} "
        "-o yaml",
        stdout_dest=artifact_dir / "artifacts" / f"{subscription_name}-installplans.yaml",
        check=False,
        log_stdout=False,
    )

    # Capture OperatorGroups (these might be shared, so capture all in namespace)
    shell.run(
        f"oc get operatorgroup -n {namespace} -o yaml",
        stdout_dest=artifact_dir / "artifacts" / f"{namespace}-operatorgroups.yaml",
        check=False,
        log_stdout=False,
    )


def _cleanup_associated_resources(subscription_name, namespace):
    """Clean up CSVs, InstallPlans, and OperatorGroups associated with the subscription"""

    # Delete CSVs owned by this subscription
    logger.info(f"Cleaning up CSVs for subscription {subscription_name}")
    shell.run(
        f"oc delete csv -n {namespace} "
        f"-l operators.coreos.com/{subscription_name}.{namespace} "
        "--ignore-not-found=true",
        check=False,
    )

    # Delete InstallPlans owned by this subscription
    logger.info(f"Cleaning up InstallPlans for subscription {subscription_name}")
    shell.run(
        f"oc delete installplan -n {namespace} "
        f"-l operators.coreos.com/{subscription_name}.{namespace} "
        "--ignore-not-found=true",
        check=False,
    )

    # Check if OperatorGroup was created specifically for this operator
    # Only delete if it's the only operator in the namespace
    result = shell.run(
        f"oc get subscription -n {namespace} --no-headers",
        check=False,
        log_stdout=False,
    )

    remaining_subscriptions = len(
        [line for line in result.stdout.strip().split("\n") if line.strip()]
    )

    if remaining_subscriptions == 0:
        logger.info(f"No remaining subscriptions in {namespace}, cleaning up OperatorGroups")
        shell.run(
            f"oc delete operatorgroup -n {namespace} --all --ignore-not-found=true",
            check=False,
        )
    else:
        logger.info(f"Other subscriptions remain in {namespace}, keeping OperatorGroups")


@task
def capture_cleanup_summary(args, ctx):
    """Generate a summary of the cleanup operation"""

    summary_file = args.artifact_dir / "artifacts" / "operator-cleanup-summary.txt"

    with open(summary_file, "w") as f:
        f.write("Operator Cleanup Summary\n")
        f.write("=" * 40 + "\n\n")

        f.write("Operators processed for cleanup:\n")
        for operator_name, namespace in ctx.all_operators:
            status = "[DRY RUN]" if args.dry_run else "[CLEANED]"
            had_subscription = (operator_name, namespace) in ctx.existing_subscriptions
            sub_status = "had subscription" if had_subscription else "no subscription found"

            f.write(
                f"  {status} Operator: {operator_name} (namespace: {namespace}) - {sub_status}\n"
            )
            f.write(f"    {status} Subscription (if exists)\n")
            f.write(f"    {status} Associated CSVs\n")
            f.write(f"    {status} Associated InstallPlans\n")
            f.write(f"    {status} OperatorGroups (if no other operators remain)\n\n")

        f.write("Summary:\n")
        f.write(f"  Total operators processed: {len(ctx.all_operators)}\n")
        f.write(f"  Operators with existing subscriptions: {len(ctx.existing_subscriptions)}\n")
        f.write(
            f"  Operators without subscriptions (resources still cleaned): {len(ctx.missing_subscriptions)}\n\n"
        )

        f.write("Captured artifacts:\n")
        for operator_name, namespace in ctx.all_operators:
            f.write(f"  - {operator_name}-csvs.yaml\n")
            f.write(f"  - {operator_name}-installplans.yaml\n")
            f.write(f"  - {namespace}-operatorgroups.yaml\n")

    return f"Generated cleanup summary at {summary_file.name}"


if __name__ == "__main__":
    run.main()
