#!/usr/bin/env python3
"""Start or stop engine-native HTTP profiling (vLLM / SGLang /start_profile)."""

from __future__ import annotations

import json

from projects.core.dsl import (
    RetryFailure,
    entrypoint,
    execute_tasks,
    retry,
    shell,
    task,
)


@entrypoint
def run(
    *,
    endpoint_url: str,
    action: str = "start",
    timeout_seconds: int = 1800,
    body: str = "",
    name: str = "",
    namespace: str = "",
    traces_dir: str = "/tmp/vllm_profile",
):
    """POST /start_profile or /stop_profile on the predictor.

    Args:
        endpoint_url: Predictor base URL (no /v1 suffix), e.g. http://name-predictor.ns.svc:8080
        action: ``start`` or ``stop``
        timeout_seconds: curl --max-time (stop/flush can take many minutes)
        body: Optional JSON string for the POST body (SGLang start options)
        name: InferenceService name; used to mkdir traces_dir in the pod
        namespace: Namespace of the InferenceService
        traces_dir: Directory created in the pod before start (vLLM torch_profiler_dir)
    """
    return execute_tasks(locals())


@task
def validate_action(args, context):
    if args.action not in ("start", "stop"):
        raise ValueError(f"action must be 'start' or 'stop', got {args.action!r}")
    base = args.endpoint_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    context.base_url = base
    context.path = "/start_profile" if args.action == "start" else "/stop_profile"
    return f"Native profiler {args.action}: {context.base_url}{context.path}"


@task
def ensure_traces_dir(args, context):
    if args.action != "start" or not args.name or not args.namespace:
        return "Skip mkdir (stop, or no pod locator)"
    traces_dir = args.traces_dir
    if not traces_dir.startswith("/tmp"):
        raise ValueError(f"traces_dir must be under /tmp, got {traces_dir}")
    pod = shell.run(
        f"oc get pod -oname "
        f"-lserving.kserve.io/inferenceservice={args.name} "
        f"-n {args.namespace} | head -1",
        check=False,
    )
    pod_name = pod.stdout.strip()
    if not pod_name:
        raise RuntimeError(f"No predictor pod found for {args.name} in {args.namespace}")
    shell.run(
        f"oc exec {pod_name} -n {args.namespace} -- mkdir -p {traces_dir}",
    )
    return f"Created {traces_dir} on {pod_name}"


@retry(attempts=8, delay=5, backoff=1.0, retry_on_exceptions=True)
@task
def call_profiler_api(args, context):
    cmd = [
        "curl",
        "-sS",
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "--max-time",
        str(args.timeout_seconds),
        "-w",
        "\nHTTP_CODE:%{http_code}",
        f"{context.base_url}{context.path}",
    ]
    if args.body:
        cmd.extend(["-d", args.body])

    result = shell.run(
        cmd,
        shell=False,
        check=False,
        timeout_seconds=float(args.timeout_seconds) + 30,
        log_stdout=True,
    )
    stdout = result.stdout or ""
    http_code = _parse_http_code(stdout)
    if http_code is None or http_code >= 500 or http_code == 0:
        raise RetryFailure(
            f"{args.action} {context.path} failed: http={http_code} rc={result.returncode} "
            f"{stdout[-500:]}"
        )
    if http_code == 404:
        raise RuntimeError(
            f"{context.path} returned 404 — start the server with --profiler-config "
            "(vLLM) or a recent SGLang build that exposes /start_profile"
        )
    if http_code >= 400:
        raise RuntimeError(f"{args.action} {context.path} HTTP {http_code}: {stdout[-500:]}")

    body = stdout.rsplit("HTTP_CODE:", 1)[0].strip()
    context.response = body
    try:
        parsed = json.loads(body) if body else {}
        context.response_json = parsed
    except json.JSONDecodeError:
        context.response_json = {}
    return f"{args.action} {context.path} HTTP {http_code}"


def _parse_http_code(stdout: str) -> int | None:
    marker = "HTTP_CODE:"
    if marker not in stdout:
        return None
    tail = stdout.rsplit(marker, 1)[-1].strip()
    try:
        return int(tail.split()[0])
    except (ValueError, IndexError):
        return None


if __name__ == "__main__":
    run.main()
