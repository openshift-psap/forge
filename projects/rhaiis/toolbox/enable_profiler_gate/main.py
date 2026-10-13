#!/usr/bin/env python3

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)


@entrypoint
def run(
    *,
    name: str,
    namespace: str,
    gate_value: str = "0",
    disable: bool = False,
    clear_traces: bool = False,
):
    """Toggle the vLLM profiler gate file on a KServe predictor pod.

    Writes (or removes) /tmp/profiler_gate inside the predictor container
    to enable or disable on-demand profiling for a running inference service.

    Args:
        name: KServe InferenceService name used to locate the predictor pod.
        namespace: Kubernetes namespace where the InferenceService is deployed.
        gate_value: Value written to the gate file (default "0").
        disable: When True, removes the gate file instead of creating it.
        clear_traces: When True, remove previous trace files before enabling the gate.
    """
    return execute_tasks(locals())


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
def toggle_profiler_gate(args, context):
    if args.disable:
        shell.run(
            f"oc exec {context.pod_name} -n {args.namespace} -- sh -c 'rm -f /tmp/profiler_gate'",
            check=False,
        )
        return f"Profiler gate disabled on {context.pod_name}"

    if args.clear_traces:
        shell.run(
            f"oc exec {context.pod_name} -n {args.namespace} -- sh -c 'rm -f /tmp/trace_*.json*'",
        )

    shell.run(
        f"oc exec {context.pod_name} -n {args.namespace} "
        f"-- sh -c 'echo {args.gate_value} > /tmp/profiler_gate'",
    )
    return f"Profiler gate enabled on {context.pod_name} (value={args.gate_value})"


if __name__ == "__main__":
    run.main()
