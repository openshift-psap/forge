#!/usr/bin/env python3

import logging
import time

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)

logger = logging.getLogger("DSL")


@entrypoint
def run(*, name: str, namespace: str, flush_timeout: int = 120, flush_poll_interval: int = 10):
    return execute_tasks(locals())


@task
def setup_directories(args, context):
    shell.mkdir("artifacts/traces")
    return "Traces directory created"


@task
def find_predictor_pod(args, context):
    result = shell.run(
        f"oc get pod -oname "
        f"-lserving.kserve.io/inferenceservice={args.name} "
        f"-n {args.namespace} "
        "| head -1",
        check=False,
    )
    pod_name = result.stdout.strip()
    if not pod_name:
        raise RuntimeError(f"No predictor pod found for {args.name} in {args.namespace}")
    context.pod_name = pod_name
    return f"Found pod: {pod_name}"


@task
def list_trace_files(args, context):
    if args.flush_poll_interval <= 0:
        raise ValueError(
            f"flush_poll_interval must be a positive integer, got {args.flush_poll_interval}"
        )

    deadline = time.monotonic() + args.flush_timeout

    while True:
        result = shell.run(
            f"oc exec {context.pod_name} -n {args.namespace} "
            "-- sh -c 'ls /tmp/trace_rank0_*.json* 2>/dev/null || echo NO_RANK0_TRACES'",
            check=False,
            log_stdout=False,
        )

        traces_missing = "NO_RANK0_TRACES" in result.stdout or not result.stdout.strip()

        if not traces_missing:
            trace_list = result.stdout.strip()
            context.trace_count = len(trace_list.splitlines())
            return f"Found {context.trace_count} rank-0 trace files"

        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(
                f"No rank-0 profiler traces found in pod {context.pod_name} "
                f"after waiting {args.flush_timeout}s"
            )

        remaining = deadline - now
        logger.info(
            f"Waiting for profiler traces to flush... "
            f"retrying in {args.flush_poll_interval}s "
            f"({remaining:.0f}s remaining of {args.flush_timeout}s timeout)"
        )
        time.sleep(min(args.flush_poll_interval, remaining))


@task
def copy_traces(args, context):
    traces_dir = args.artifact_dir / "artifacts/traces"
    shell.run(
        'bash -o pipefail -c "'
        f"oc exec {context.pod_name} -n {args.namespace}"
        " -- sh -c 'cd /tmp && tar cf - trace_rank0_*.json*'"
        f' | tar --no-same-owner -xf - -C {traces_dir}"',
    )
    copied = list(traces_dir.glob("trace_rank0_*.json*"))
    return f"Copied {len(copied)} rank-0 trace files to {traces_dir}"


if __name__ == "__main__":
    run.main()
