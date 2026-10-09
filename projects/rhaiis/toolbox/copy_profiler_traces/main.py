#!/usr/bin/env python3

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)
from projects.core.library.ci import add_notification_file


@entrypoint
def run(*, name: str, namespace: str):
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
    result = shell.run(
        f"oc exec {context.pod_name} -n {args.namespace} "
        "-- sh -c 'ls /tmp/trace_rank0_*.json* 2>/dev/null || echo NO_RANK0_TRACES'",
        check=False,
        log_stdout=False,
    )

    if "NO_RANK0_TRACES" not in result.stdout and result.stdout.strip():
        trace_list = result.stdout.strip()
        context.trace_count = len(trace_list.splitlines())
        return f"Found {context.trace_count} rank-0 trace files"

    if _profiler_window_missed(args, context):
        # The benchmark window ended before the profiling range was reached, so
        # the profiler never armed and no trace files can exist. The benchmark
        # results are unaffected: surface a notification instead of failing the
        # run.
        add_notification_file(
            "PROFILER_WINDOW_MISSED",
            f"No profiler traces in {context.pod_name}: the profiler gate never "
            f"armed during the benchmark window (the engine did not reach the "
            f"profiling range call count before the window closed). Benchmark "
            f"results are unaffected.",
        )
        context.trace_count = 0
        return "No rank-0 trace files: profiler window missed (notification added)"

    raise RuntimeError(f"No rank-0 profiler traces found in pod {context.pod_name}")


def _profiler_window_missed(args, context) -> bool:
    """Distinguish "the profiler never armed" from "it armed but produced no traces".

    The injected profiler logs "[profiler] Starting profiler" once per armed
    range and "Exported trace" once per exported file. A pod restart wipes both
    the trace files and the logs, so a non-zero restart count is treated as
    "profiler did run" to keep engine-death failures fatal.
    """
    restarts = shell.run(
        f"oc get {context.pod_name} -n {args.namespace} "
        "-o jsonpath='{.status.containerStatuses[?(@.name==\"kserve-container\")].restartCount}'",
        check=False,
    )
    if restarts.stdout.strip() not in ("", "0"):
        return False

    started = shell.run(
        f"oc logs {context.pod_name} -n {args.namespace} -c kserve-container "
        "| grep -c '\\[profiler\\] Starting profiler'",
        check=False,
    )
    return started.stdout.strip() == "0"


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
