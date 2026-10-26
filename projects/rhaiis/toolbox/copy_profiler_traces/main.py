#!/usr/bin/env python3

import gzip
import hashlib
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    retry,
    shell,
    task,
)

logger = logging.getLogger(__name__)
_MIN_FREE_BYTES = 64 * 1024 * 1024
_TRANSFER_CHUNK_BYTES = 8 * 1024 * 1024
_CHUNK_ATTEMPTS = 3
_CHUNK_ATTEMPT_TIMEOUT_SECONDS = 60
_READ_CHUNK_SCRIPT = (
    "import sys\n"
    "with open(sys.argv[1], 'rb') as trace:\n"
    "    trace.seek(int(sys.argv[2]))\n"
    "    sys.stdout.buffer.write(trace.read(int(sys.argv[3])))\n"
)


@entrypoint
def run(*, name: str, namespace: str, transfer_timeout_seconds: int = 300):
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
        [
            "oc",
            "exec",
            context.pod_name,
            "-n",
            args.namespace,
            "-c",
            "kserve-container",
            "--",
            "find",
            "/tmp",
            "-maxdepth",
            "1",
            "-type",
            "f",
            "-name",
            "trace_rank0_*.json*",
            "-print",
        ],
        shell=False,
        log_stdout=False,
    )
    paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    if not paths:
        raise RuntimeError(f"No rank-0 profiler traces found in pod {context.pod_name}")

    for path in paths:
        if (
            path.parent != Path("/tmp")
            or not path.name.startswith("trace_rank0_")
            or not path.name.endswith((".json", ".json.gz"))
        ):
            raise ValueError(f"Unexpected profiler trace path: {path}")

    output_names = [path.name.removesuffix(".gz") for path in paths]
    if len(output_names) != len(set(output_names)):
        raise RuntimeError("Both compressed and uncompressed copies of a rank-0 trace exist")

    context.trace_files = sorted(path.name for path in paths)
    context.trace_count = len(context.trace_files)
    return f"Found {context.trace_count} rank-0 trace files"


def _oc_exec(pod_name: str, namespace: str, *command: str) -> list[str]:
    return [
        "oc",
        "exec",
        pod_name,
        "-n",
        namespace,
        "-c",
        "kserve-container",
        "--",
        *command,
    ]


def _remote_text(pod_name: str, namespace: str, *command: str, timeout_seconds: int = 60) -> str:
    result = subprocess.run(
        _oc_exec(pod_name, namespace, *command),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Remote profiler trace command {command[0]} failed "
            f"(exit {result.returncode}): {result.stderr.strip()[-1000:]}"
        )
    return result.stdout.strip()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as trace:
        while chunk := trace.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_remote_chunk(
    pod_name: str,
    namespace: str,
    source: str,
    offset: int,
    length: int,
    deadline: float,
) -> bytes:
    """Retry bounded reads because oc exec can exit successfully with truncated stdout."""
    command = _oc_exec(
        pod_name,
        namespace,
        "python3",
        "-c",
        _READ_CHUNK_SCRIPT,
        source,
        str(offset),
        str(length),
    )
    last_error = ""

    for attempt in range(1, _CHUNK_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Profiler trace transfer for {source} exceeded its timeout")

        try:
            transfer = subprocess.run(
                command,
                capture_output=True,
                timeout=min(remaining, _CHUNK_ATTEMPT_TIMEOUT_SECONDS),
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = "oc exec timed out"
        else:
            if transfer.returncode:
                stderr = transfer.stderr.decode("utf-8", errors="replace").strip()[-1000:]
                last_error = f"oc exec exit {transfer.returncode}: {stderr}"
            elif len(transfer.stdout) != length:
                last_error = f"received {len(transfer.stdout)} of {length} bytes"
            else:
                return transfer.stdout

        logger.warning(
            "Profiler trace chunk at offset %d failed (attempt %d/%d): %s",
            offset,
            attempt,
            _CHUNK_ATTEMPTS,
            last_error,
        )

    raise RuntimeError(
        f"Profiler trace transfer for {source} failed at offset {offset} "
        f"after {_CHUNK_ATTEMPTS} attempts: {last_error}"
    )


def _copy_one_trace(
    pod_name: str, namespace: str, filename: str, traces_dir: Path, timeout_seconds: int
) -> Path:
    source = f"/tmp/{filename}"
    source_is_gzip = filename.endswith(".gz")
    destination = traces_dir / filename.removesuffix(".gz")
    partial = destination.with_name(f"{destination.name}.partial")
    compressed_partial = traces_dir / f"{filename}.partial" if source_is_gzip else None
    transfer_target = compressed_partial or partial
    source_size = int(_remote_text(pod_name, namespace, "stat", "-c", "%s", source))
    if source_size <= 0:
        raise RuntimeError(f"Profiler trace {filename} is empty")

    free_bytes = shutil.disk_usage(traces_dir).free
    logger.info(
        "Profiler trace %s: source=%d bytes, workspace free=%d bytes",
        filename,
        source_size,
        free_bytes,
    )
    if free_bytes < source_size + _MIN_FREE_BYTES:
        raise RuntimeError(
            f"Artifact workspace has only {free_bytes} bytes free before copying "
            f"{source_size}-byte trace {filename}"
        )

    deadline = time.monotonic() + timeout_seconds
    try:
        with transfer_target.open("wb") as output:
            for offset in range(0, source_size, _TRANSFER_CHUNK_BYTES):
                length = min(_TRANSFER_CHUNK_BYTES, source_size - offset)
                output.write(
                    _read_remote_chunk(pod_name, namespace, source, offset, length, deadline)
                )
        if transfer_target.stat().st_size != source_size:
            raise RuntimeError(
                f"Profiler trace transfer for {filename} produced "
                f"{transfer_target.stat().st_size} of {source_size} bytes"
            )

        final_source_size = int(_remote_text(pod_name, namespace, "stat", "-c", "%s", source))
        if source_size != final_source_size:
            raise RuntimeError(f"Profiler trace {filename} changed size during transfer")

        expected_digest = _remote_text(
            pod_name, namespace, "sha256sum", source, timeout_seconds=timeout_seconds
        ).split()[0]
        actual_digest = _file_digest(transfer_target)
        if actual_digest != expected_digest:
            raise RuntimeError(f"Profiler trace {filename} failed SHA-256 verification")

        if compressed_partial is not None:
            with gzip.open(compressed_partial, "rb") as archive, partial.open("wb") as output:
                shutil.copyfileobj(archive, output, length=8 * 1024 * 1024)
            if partial.stat().st_size == 0:
                raise RuntimeError(f"Profiler trace {filename} decoded to an empty file")

        os.replace(partial, destination)
        logger.info(
            "Verified profiler trace %s (%d JSON bytes)",
            destination,
            destination.stat().st_size,
        )
        return destination
    finally:
        partial.unlink(missing_ok=True)
        if compressed_partial is not None:
            compressed_partial.unlink(missing_ok=True)


@retry(attempts=3, delay=2, backoff=2, retry_on_exceptions=True)
@task
def copy_traces(args, context):
    traces_dir = args.artifact_dir / "artifacts/traces"
    copied = [
        _copy_one_trace(
            context.pod_name,
            args.namespace,
            filename,
            traces_dir,
            args.transfer_timeout_seconds,
        )
        for filename in context.trace_files
    ]
    context.copied_traces = [str(path) for path in copied]
    return f"Copied and verified {len(copied)} rank-0 trace files to {traces_dir}"


if __name__ == "__main__":
    run.main()
