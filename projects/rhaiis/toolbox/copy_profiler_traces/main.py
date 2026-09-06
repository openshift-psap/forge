#!/usr/bin/env python3

from __future__ import annotations

import re
import shutil
from pathlib import Path

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)

_SAFE_DIR = re.compile(r"^/tmp(/[A-Za-z0-9._-]+)*$")
_SAFE_GLOB = re.compile(r"^[A-Za-z0-9._*?\[\]-]+$")


def normalize_trace_name(original: str, run_label: str = "") -> str:
    """Make engine-native filenames match the webhook/S3 convention.

    S3 upload looks for ``trace_*rank0*``. vLLM writes ``*.pt.trace.json.gz``;
    SGLang writes ``*.trace.json.gz``. Prefix those so the existing uploader
    still finds rank-0 artifacts.
    """
    name = Path(original).name
    if run_label and f"run{run_label}" not in name:
        name = f"run{run_label}_{name}"
    if "rank" not in name.lower():
        name = f"rank0_{name}"
    if not name.startswith("trace"):
        name = f"trace_{name}"
    return name


@entrypoint
def run(
    *,
    name: str,
    namespace: str,
    remote_dir: str = "/tmp",
    file_glob: str = "trace_*.json*",
    run_label: str = "",
):
    """Copy profiler traces from the predictor pod.

    Args:
        name: KServe InferenceService name
        namespace: Namespace of the InferenceService
        remote_dir: Directory inside the pod (webhook: /tmp, native: torch_profiler_dir)
        file_glob: Files to tar. Use ``*`` to copy the whole remote_dir.
        run_label: Optional gate/profile label injected into copied filenames
    """
    return execute_tasks(locals())


@task
def setup_directories(args, context):
    shell.mkdir("artifacts/traces")
    return "Traces directory created"


@task
def validate_remote_path(args, context):
    if not _SAFE_DIR.match(args.remote_dir):
        raise ValueError(f"remote_dir must be an absolute /tmp path, got {args.remote_dir!r}")
    if args.file_glob != "*" and not _SAFE_GLOB.match(args.file_glob):
        raise ValueError(f"Unsafe file_glob: {args.file_glob!r}")
    return f"Copying {args.file_glob} from {args.remote_dir}"


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
    if args.file_glob == "*":
        ls_cmd = f"ls -A {args.remote_dir} 2>/dev/null || echo NO_TRACES"
    else:
        ls_cmd = f"ls {args.remote_dir}/{args.file_glob} 2>/dev/null || echo NO_TRACES"
    result = shell.run(
        f"oc exec {context.pod_name} -n {args.namespace} -- sh -c '{ls_cmd}'",
        check=False,
        log_stdout=False,
    )

    if "NO_TRACES" in result.stdout or not result.stdout.strip():
        raise RuntimeError(f"No profiler traces found in pod {context.pod_name}:{args.remote_dir}")

    trace_list = result.stdout.strip()
    context.trace_count = len([line for line in trace_list.splitlines() if line.strip()])
    return f"Found {context.trace_count} trace files"


@task
def copy_traces(args, context):
    traces_dir = args.artifact_dir / "artifacts/traces"
    tar_args = "." if args.file_glob == "*" else args.file_glob
    shell.run(
        'bash -o pipefail -c "'
        f"oc exec {context.pod_name} -n {args.namespace}"
        f" -- sh -c 'cd {args.remote_dir} && tar cf - {tar_args}'"
        f' | tar --no-same-owner -xf - -C {traces_dir}"',
    )
    copied = _flatten_and_normalize(traces_dir, args.run_label)
    return f"Copied {len(copied)} trace files to {traces_dir}"


def _flatten_and_normalize(traces_dir: Path, run_label: str) -> list[Path]:
    files = [p for p in traces_dir.rglob("*") if p.is_file()]
    dest_files: list[Path] = []
    for src in files:
        dest = traces_dir / normalize_trace_name(src.name, run_label)
        if src.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            shutil.move(str(src), str(dest))
        dest_files.append(dest)
    for path in sorted(traces_dir.rglob("*"), reverse=True):
        if path.is_dir() and path != traces_dir and not any(path.iterdir()):
            path.rmdir()
    return dest_files


if __name__ == "__main__":
    run.main()
