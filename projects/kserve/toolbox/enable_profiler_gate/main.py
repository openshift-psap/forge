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
    pod_selector: str,
    namespace: str,
    container: str = "kserve-container",
    gate_value: str = "0",
    disable: bool = False,
    clear_traces: bool = False,
):
    """Toggle the vLLM profiler gate file on a KServe predictor pod.

    Writes (or removes) /tmp/profiler_gate inside the predictor container
    to enable or disable on-demand profiling for a running inference service.

    Args:
        pod_selector: Label selector matching the predictor pod
            (e.g. "serving.kserve.io/inferenceservice=<isvc-name>").
        namespace: Kubernetes namespace where the inference service is deployed.
        container: Name of the predictor container holding the gate file.
        gate_value: Value written to the gate file (default "0").
        disable: When True, removes the gate file instead of creating it.
        clear_traces: When True, remove previous trace files before enabling the gate.
    """
    return execute_tasks(locals())


@task
def find_predictor_pod(args, context):
    result = shell.run(
        f"oc get pod -oname -l{args.pod_selector} -n {args.namespace} | head -1",
        check=False,
    )
    pod_name = result.stdout.strip()
    if not pod_name:
        raise RuntimeError(
            f"No predictor pod found for selector '{args.pod_selector}' in {args.namespace}"
        )
    context.pod_name = pod_name
    return f"Found pod: {pod_name}"


@task
def toggle_profiler_gate(args, context):
    if args.disable:
        shell.run(
            f"oc exec {context.pod_name} -n {args.namespace} -c {args.container} "
            f"-- sh -c 'rm -f /tmp/profiler_gate'",
            check=False,
        )
        return f"Profiler gate disabled on {context.pod_name}"

    if args.clear_traces:
        shell.run(
            f"oc exec {context.pod_name} -n {args.namespace} -c {args.container} "
            f"-- sh -c 'rm -f /tmp/trace_*.json*'",
        )

    shell.run(
        f"oc exec {context.pod_name} -n {args.namespace} -c {args.container} "
        f"-- sh -c 'echo {args.gate_value} > /tmp/profiler_gate'",
    )
    return f"Profiler gate enabled on {context.pod_name} (value={args.gate_value})"


if __name__ == "__main__":
    run.main()
