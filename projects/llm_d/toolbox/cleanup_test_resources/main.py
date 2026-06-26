#!/usr/bin/env python3

from __future__ import annotations

import logging
import subprocess

from projects.core.dsl import entrypoint, execute_tasks, retry, shell, task
from projects.core.dsl.utils.k8s import oc, oc_get_json, oc_resource_exists

logger = logging.getLogger("DSL")


@entrypoint
def run(
    *,
    namespace: str,
    inference_service_name: str,
    smoke_pod_name: str | None = None,
    benchmark_job_name: str | None = None,
    cleanup_all_llm_d_resources: bool = False,
) -> int:
    """
    Clean up test resources including jobs, PVCs, and llminferenceservice.

    Args:
        namespace: Namespace containing the resources
        inference_service_name: Name of the LLM inference service
        smoke_pod_name: Name of the smoke test pod (optional)
        benchmark_job_name: Name of the benchmark job (optional)
        cleanup_all_llm_d_resources: Clean up all llm_d labeled resources
    """

    execute_tasks(locals())
    return 0


@task
def setup_directories(args, ctx):
    """Create artifact directories"""

    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    ctx.benchmark_name = args.benchmark_job_name
    if not ctx.benchmark_name and not args.cleanup_all_llm_d_resources:
        ctx.benchmark_name = "guidellm-benchmark"

    return f"Prepared cleanup for namespace {args.namespace}"


@task
def delete_smoke_test_pod(args, ctx):
    """Delete smoke test pod if specified"""

    if not args.smoke_pod_name:
        return "No smoke pod specified, skipping"

    _best_effort_delete(
        "smoke helper pod",
        "delete",
        "pod",
        args.smoke_pod_name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
    )

    return f"Deleted smoke test pod {args.smoke_pod_name}"


@task
def delete_benchmark_resources(args, ctx):
    """Delete benchmark job, PVC and related resources"""

    if not ctx.benchmark_name:
        return "No benchmark resources specified, skipping"

    _best_effort_delete(
        "benchmark helper job and pvc",
        "delete",
        "job,pvc",
        ctx.benchmark_name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
    )
    _best_effort_delete(
        "benchmark helper copy pod",
        "delete",
        "pod",
        f"{ctx.benchmark_name}-copy",
        "-n",
        args.namespace,
        "--ignore-not-found=true",
    )

    # For runtime cleanup, also handle the default benchmark name
    if args.cleanup_all_llm_d_resources and ctx.benchmark_name != "guidellm-benchmark":
        _best_effort_delete(
            "default benchmark helper job and pvc",
            "delete",
            "job,pvc",
            "guidellm-benchmark",
            "-n",
            args.namespace,
            "--ignore-not-found=true",
        )
        _best_effort_delete(
            "default benchmark helper copy pod",
            "delete",
            "pod",
            "guidellm-benchmark-copy",
            "-n",
            args.namespace,
            "--ignore-not-found=true",
        )

    return f"Deleted benchmark resources for {ctx.benchmark_name}"


@task
def delete_llm_d_labeled_resources(args, ctx):
    """Delete all llm_d labeled resources when cleanup_all is enabled"""

    if not args.cleanup_all_llm_d_resources:
        return "Cleanup all llm_d resources disabled, skipping"

    _best_effort_delete(
        "llm_d jobs",
        "delete",
        "job",
        "-n",
        args.namespace,
        "-l",
        "forge.openshift.io/project=llm_d",
        "--ignore-not-found=true",
    )
    _best_effort_delete(
        "llm_d pods",
        "delete",
        "pod",
        "-n",
        args.namespace,
        "-l",
        "forge.openshift.io/project=llm_d",
        "--ignore-not-found=true",
    )
    _best_effort_delete(
        "llm_d non-preserved pvcs",
        "delete",
        "pvc",
        "-n",
        args.namespace,
        "-l",
        "forge.openshift.io/project=llm_d,forge.openshift.io/preserve!=true",
        "--ignore-not-found=true",
    )

    return "Deleted all llm_d labeled resources"


@task
def delete_inference_service(args, ctx):
    """Delete the LLM inference service"""

    _best_effort_delete(
        "llminferenceservice",
        "delete",
        "llminferenceservice",
        args.inference_service_name,
        "-n",
        args.namespace,
        "--ignore-not-found=true",
    )

    return f"Deleted llminferenceservice {args.inference_service_name}"


@retry(attempts=90, delay=10, backoff=1.0)
@task
def wait_for_inference_service_deletion(args, ctx):
    """Wait for the llminferenceservice to be deleted"""

    if not oc_resource_exists(
        "llminferenceservice", args.inference_service_name, namespace=args.namespace
    ):
        return f"llminferenceservice/{args.inference_service_name} deleted from {args.namespace}"

    return (
        False,
        f"Waiting for llminferenceservice/{args.inference_service_name} deletion in {args.namespace}",
    )


@retry(attempts=90, delay=10, backoff=1.0)
@task
def wait_for_workload_pods_deletion(args, ctx):
    """Wait for all llm-d workload pods to be deleted"""

    # Show current pods in plain text with logs enabled
    shell.run(
        f"oc get pods -n {args.namespace} -l app.kubernetes.io/name={args.inference_service_name}",
        check=False,
        log_stdout=True,
    )

    shell.run(
        f"oc get pods -n {args.namespace} -l app.kubernetes.io/name={args.inference_service_name} -o json",
        check=False,
        log_stdout=False,
    )

    if _llm_d_pods_gone(args.namespace, args.inference_service_name):
        return f"All llm-d workload pods deleted from {args.namespace}"

    return (False, f"Waiting for llm-d workload pods deletion in {args.namespace}")


def _best_effort_delete(description: str, *oc_args: str) -> None:
    """Best effort deletion of Kubernetes resources with timeout"""
    try:
        oc(*oc_args, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out deleting %s: oc %s", description, " ".join(oc_args))


def _llm_d_pods_gone(namespace: str, inference_service_name: str) -> bool:
    """Check if all llm-d workload pods are gone"""
    payload = oc_get_json(
        "pods",
        namespace=namespace,
        selector=f"app.kubernetes.io/name={inference_service_name}",
        ignore_not_found=True,
    )
    return not payload or not payload.get("items")


if __name__ == "__main__":
    run.main()
