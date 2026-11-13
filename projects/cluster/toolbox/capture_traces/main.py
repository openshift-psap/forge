#!/usr/bin/env python3

"""
Capture Traces Toolbox

Snapshots the sanitized OTLP spool from a capture's collector, transfers it,
validates it, partitions traces per benchmark window, and publishes one trace
bundle per window below the owning Caliper test node.

The collector sanitizes before its first spool write. Capture reapplies that
policy before publishing archives. Records are partitioned by
original trace ID while keeping the native OTLP shape (kinds, events, links,
status, resource and scope data), so the archive stays replayable.

A stable file size or a quiet collector does not prove producers finished; the
drain method, timeouts and any interruption are recorded so an incomplete
capture is visible.

Can be run standalone:
    ./bin/run_toolbox cluster capture_traces \\
        --namespace my-ns --capture-id cap-123 --windows-file /path/windows.json \\
        --output-dir /path/out
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from projects.caliper.engine.tracing import bundle as bundle_mod
from projects.caliper.engine.tracing.analysis import (
    TraceIndex,
    TraceValidationError,
    analyze_quality,
    assign_traces_to_windows,
    build_trace_index,
    count_spans,
    iter_span_records,
    read_archive_records,
)
from projects.cluster.library.tracing.collection import (
    OWNERSHIP_LABEL_KEY,
    SPOOL_FILENAME,
    SPOOL_MOUNT_PATH,
)
from projects.cluster.library.tracing.models import (
    SUMMARY_SCHEMA_VERSION,
    CollectionQuality,
    CollectionStatus,
    TraceWindow,
    build_service_name,
)
from projects.cluster.library.tracing.sanitizer import (
    SANITIZATION_POLICY,
    SANITIZATION_POLICY_VERSION,
    sanitize_export_request,
)
from projects.core.dsl import always, entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils import write_json
from projects.core.dsl.utils.k8s import oc, oc_cp_from_pod, oc_exec

logger = logging.getLogger("DSL")

# Number of consecutive identical spool sizes that counts as settled.
SETTLE_STABLE_SAMPLES = 3
SETTLE_SAMPLE_DELAY = 5
# Upper bound on retry attempts; the configured settle deadline is the real
# limit, so a larger configured budget is not cut short by the attempt count.
SETTLE_MAX_ATTEMPTS = 10_000


class TraceCaptureError(RuntimeError):
    """Raised when capture inputs or bounds prevent a trustworthy bundle."""


@entrypoint
def run(
    namespace: str,
    capture_id: str,
    windows_file: str | Path,
    output_dir: str | Path,
    *,
    project: str = "",
    job_id: str = "",
    deployment_id: str = "",
    mode: str = "standard",
    sample_ratio: float = 0.1,
    collector_image: str = "",
    instrumentation_roles: str = "",
    settle_timeout_seconds: int = 300,
    max_bundle_bytes: int = 2 * 1024 * 1024 * 1024,
    interrupted: bool = False,
) -> int:
    """
    Capture and publish trace bundles for a capture's benchmark windows.

    Args:
        namespace: Namespace holding the collector and spool
        capture_id: Capture identity to snapshot
        windows_file: Persisted JSON manifest of benchmark windows (list of TraceWindow)
        output_dir: Directory to publish bundles under (the test artifact root)
        project: Owning FORGE project name
        job_id: FORGE job / local invocation identity
        deployment_id: Stable serving deployment identity
        mode: Tracing mode that was active for the capture
        sample_ratio: Sampling ratio that was active for the capture
        collector_image: Collector image used for the capture
        instrumentation_roles: Comma-separated instrumented component roles
        settle_timeout_seconds: Bounded budget for waiting on the spool to settle
        max_bundle_bytes: Maximum archive size accepted before streaming is enforced
        interrupted: Set when a producer/collector interruption is known
    """
    execute_tasks(locals())
    return 0


def _spool_selector(capture_id: str) -> str:
    return f"{OWNERSHIP_LABEL_KEY}={capture_id}"


def _collector_pod(namespace: str, capture_id: str) -> str:
    result = oc(
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        _spool_selector(capture_id),
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    pod = result.stdout.strip()
    if not pod:
        raise RuntimeError(f"no collector pod found for capture {capture_id}")
    return pod


def _spool_bytes(namespace: str, pod: str) -> int:
    """Return the spool size in bytes, or 0 when the file does not exist yet.

    The shell falls back to ``echo 0`` for a missing file, so unparsable output
    means the stat itself failed (unreadable or rotated spool). That must raise
    rather than look identical to an empty capture.
    """
    result = oc_exec(
        "sh",
        "-c",
        f"stat -c %s {SPOOL_MOUNT_PATH}/{SPOOL_FILENAME} 2>/dev/null || echo 0",
        namespace=namespace,
        pod=pod,
        container="spool-reader",
    )
    raw = result.stdout.strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"could not read collector spool size (got {raw!r}); the spool may be "
            "unreadable or rotated"
        ) from exc


def _spool_checksum(namespace: str, pod: str) -> str:
    """Return the remote spool's sha256, or an empty string when unavailable.

    Uses the spool-reader image so the collector image needs no shell or hashing
    tool. A missing checksum is not fatal on its own; the transfer still
    verifies byte counts.
    """
    result = oc_exec(
        "sh",
        "-c",
        f"sha256sum {SPOOL_MOUNT_PATH}/{SPOOL_FILENAME} 2>/dev/null | cut -d' ' -f1",
        namespace=namespace,
        pod=pod,
        container="spool-reader",
    )
    return result.stdout.strip()


@task
def validate_inputs(args, ctx):
    """Validate inputs and load the persisted benchmark-window manifest."""
    ctx.output_dir = Path(args.output_dir)
    ctx.output_dir.mkdir(parents=True, exist_ok=True)

    windows_path = Path(args.windows_file)
    if not windows_path.is_file():
        raise FileNotFoundError(f"windows manifest not found: {windows_path}")
    try:
        raw = json.loads(windows_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"windows manifest is not valid JSON: {windows_path}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("windows manifest must be a non-empty list")

    ctx.windows = [TraceWindow.from_dict(entry) for entry in raw]
    ctx.capture_id = args.capture_id
    ctx.namespace = args.namespace
    ctx.project = args.project
    ctx.job_id = args.job_id
    ctx.deployment_id = args.deployment_id
    ctx.mode = args.mode
    ctx.sample_ratio = args.sample_ratio
    ctx.collector_image = args.collector_image
    ctx.instrumentation_roles = [r for r in args.instrumentation_roles.split(",") if r]
    ctx.interrupted = bool(args.interrupted)
    ctx.settle_timeout_seconds = int(args.settle_timeout_seconds)
    ctx.max_bundle_bytes = int(args.max_bundle_bytes)

    return f"Loaded {len(ctx.windows)} benchmark window(s)"


@task
def resolve_spool(args, ctx):
    """Locate the collector pod and record the spool's initial size."""
    ctx.collector_pod = _collector_pod(ctx.namespace, ctx.capture_id)
    ctx.spool_bytes = _spool_bytes(ctx.namespace, ctx.collector_pod)
    # Start the settle budget only once collection is actually being drained.
    ctx.settle_deadline = time.monotonic() + ctx.settle_timeout_seconds
    if ctx.spool_bytes <= 0:
        return "Spool is empty (expected for zero/late sampling)"
    return f"Spool present ({ctx.spool_bytes} bytes)"


@retry(attempts=SETTLE_MAX_ATTEMPTS, delay=SETTLE_SAMPLE_DELAY, backoff=1.0)
@task
def wait_for_settle(args, ctx):
    """Poll the spool until its size is stable for several samples.

    Uses observed spool bytes rather than a fixed sleep, and accounts for
    producer batching. The wait is bounded by ``settle_timeout_seconds``; when
    the deadline expires the capture is recorded as interrupted instead of
    claiming a graceful drain.
    """
    deadline = getattr(ctx, "settle_deadline", None)
    if deadline is not None and time.monotonic() >= deadline:
        ctx.interrupted = True
        return (
            f"Settle deadline of {ctx.settle_timeout_seconds}s expired; "
            "capture recorded as interrupted"
        )

    current = _spool_bytes(ctx.namespace, ctx.collector_pod)
    ctx.last_size = getattr(ctx, "last_size", None)
    ctx.stable_samples = getattr(ctx, "stable_samples", 0)

    if ctx.spool_bytes == 0 and current == 0:
        # Nothing to wait for; an empty capture is a valid recorded outcome.
        return "Spool empty; nothing to settle"

    if current == ctx.last_size:
        ctx.stable_samples += 1
    else:
        ctx.stable_samples = 0
    ctx.last_size = current

    if ctx.stable_samples >= SETTLE_STABLE_SAMPLES:
        ctx.spool_bytes = current
        return f"Spool settled at {current} bytes"
    return (False, f"spool size {current} not yet stable")


@task
def transfer_spool(args, ctx):
    """Copy the spool file from the spool-reader sidecar and verify its size.

    Uses the spool-reader container, which has read-only access and does not
    rely on the collector image containing tar, a shell, or Python.
    """
    if ctx.spool_bytes <= 0:
        ctx.local_spool = None
        return "No spool data to transfer"

    # The raw spool may still contain unsanitized prompt/completion content.
    # It must never be written below the artifact directory, even transiently.
    temp_dir = Path(tempfile.mkdtemp(prefix="forge-trace-spool-"))
    local_spool = temp_dir / "raw-otlp.jsonl"
    ctx.spool_temp_dir = temp_dir
    oc_cp_from_pod(
        f"{SPOOL_MOUNT_PATH}/{SPOOL_FILENAME}",
        local_spool,
        namespace=ctx.namespace,
        pod=ctx.collector_pod,
        container="spool-reader",
    )

    copied = local_spool.stat().st_size
    if copied < ctx.spool_bytes:
        raise RuntimeError(f"spool transfer truncated: copied {copied} of {ctx.spool_bytes} bytes")

    remote_sha = _spool_checksum(ctx.namespace, ctx.collector_pod)
    if remote_sha:
        local_sha = _sha256_file(local_spool)
        if local_sha != remote_sha:
            raise RuntimeError(
                "spool transfer checksum mismatch: copied bytes do not match the "
                "collector spool; refusing to publish an untrusted archive"
            )
    ctx.local_spool = local_spool
    return f"Transferred spool ({copied} bytes)"


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@task
def sanitize_and_publish(args, ctx):
    """Sanitize, index, partition, and publish one bundle per window.

    Sanitized records are spooled to a private temporary file and partitioned
    with a streaming pass, so memory use does not grow with the captured
    payload. The raw spool is never copied below the artifact directory.
    """
    windows = ctx.windows
    if ctx.local_spool is None:
        for window in windows:
            _publish_empty_bundle(ctx, window, reason="no spans captured")
        ctx.status = CollectionStatus.EMPTY
        return "Published empty bundles (no spans captured)"

    sanitized_path = _sanitize_spool(ctx)
    try:
        index = build_trace_index(_iter_span_records_from_file(sanitized_path))
    except TraceValidationError:
        _retain_invalid_capture(ctx, sanitized_path)
        raise
    quality = analyze_quality(index)
    quality.partial_records = int(getattr(ctx, "partial_records", 0) or 0)
    if ctx.interrupted:
        quality.interrupted = True

    assignment = assign_traces_to_windows(index, windows)
    _publish_unassigned(ctx, sanitized_path, assignment.unassigned)
    total_trace_count = len(index.traces)
    total_span_count = count_spans(index)

    partitioned = _partition_spool(ctx, sanitized_path, assignment)
    published = 0
    empty_windows = 0
    component_failures = 0
    for window in windows:
        trace_ids = set(assignment.assigned.get(window.test_id, []))
        window_index = TraceIndex(
            traces={trace_id: index.traces[trace_id] for trace_id in trace_ids}
        )
        observed = sorted(
            {span.service_name for span in window_index.all_spans() if span.service_name}
        )
        component_error = _epp_capture_error(ctx, window, window_index)
        span_count = _publish_bundle(
            ctx,
            window,
            records_path=partitioned.get(window.test_id),
            trace_ids=trace_ids,
            quality=quality,
            unassigned=assignment.unassigned,
            observed=observed,
            total_trace_count=total_trace_count,
            total_span_count=total_span_count,
            component_error=component_error,
        )
        component_failures += bool(component_error)
        published += 1
        if span_count == 0:
            empty_windows += 1

    # A window whose bundle held no assigned spans is recorded empty, and any
    # such window makes the run's status EMPTY. Callers gate full-sampling
    # acceptance on that, so an instrumentation/endpoint miss is not silent.
    attribution_failed = any(reason != "outside-window" for _, reason in assignment.unassigned)
    if attribution_failed or component_failures:
        ctx.status = CollectionStatus.FAILED
    elif ctx.interrupted:
        ctx.status = CollectionStatus.INCOMPLETE
    elif empty_windows:
        ctx.status = CollectionStatus.EMPTY
    else:
        ctx.status = CollectionStatus.COLLECTED
    ctx.published = published
    if ctx.interrupted:
        return f"Published {published} incomplete bundle(s) after an interrupted drain"
    if empty_windows:
        return f"Published {published} bundle(s), {empty_windows} with no assigned spans"
    return f"Published {published} bundle(s)"


def _epp_capture_error(ctx, window: TraceWindow, index: TraceIndex) -> str:
    """At full sampling, require both services and a propagated EPP parent."""
    if "epp" not in ctx.instrumentation_roles or ctx.sample_ratio < 1.0:
        return ""
    deployment_id = window.deployment_id or ctx.deployment_id
    epp = build_service_name(deployment_id, "epp")
    vllm = build_service_name(deployment_id, "vllm")
    observed = {span.service_name for span in index.all_spans()}
    missing = {epp, vllm} - observed
    if missing:
        return "missing expected services at full sampling: " + ", ".join(sorted(missing))
    for spans in index.traces.values():
        by_id = {span.span_id: span for span in spans}
        for span in spans:
            parent = by_id.get(span.parent_span_id)
            if span.service_name == vllm and parent is not None and parent.service_name == epp:
                return ""
    return "no EPP-to-vLLM parent relationship at full sampling; check context propagation"


def _retain_invalid_capture(ctx, sanitized_path):
    """Preserve conflicting sanitized records without creating an export bundle."""
    limit = getattr(ctx, "max_bundle_bytes", 0)
    if limit and sanitized_path.stat().st_size > limit:
        raise TraceCaptureError("invalid capture exceeds diagnostic size limit")
    directory = ctx.output_dir / "trace-diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "invalid.otlp.jsonl.gz"
    with sanitized_path.open("rb") as source, gzip.open(archive, "wb") as target:
        shutil.copyfileobj(source, target)
    write_json(
        directory / "validation.json",
        {
            "reason": "trace-validation-failed",
            "archive": archive.name,
            "sha256": _sha256_file(archive),
        },
    )
    ctx.status = CollectionStatus.FAILED


def _publish_unassigned(ctx, sanitized_path, unassigned):
    """Retain unowned data outside test bundles so it is never linked to a run."""
    diagnostics = ctx.output_dir / "trace-diagnostics"
    if not unassigned and not diagnostics.exists():
        return
    diagnostics.mkdir(parents=True, exist_ok=True)
    ids = {trace_id for trace_id, _ in unassigned}
    archive = diagnostics / "unassigned.otlp.jsonl.gz"
    pending = archive.with_suffix(".tmp")
    size = 0
    limit = getattr(ctx, "max_bundle_bytes", 0)
    try:
        with gzip.open(pending, "wt", encoding="utf-8") as handle:
            for record in read_archive_records(sanitized_path):
                for part in _partition_records([record], ids):
                    line = json.dumps(part) + "\n"
                    size += len(line.encode())
                    if limit and size > limit:
                        raise TraceCaptureError("unassigned diagnostics exceed max_bundle_bytes")
                    handle.write(line)
        pending.replace(archive)
    finally:
        pending.unlink(missing_ok=True)
    write_json(
        diagnostics / "attribution.json",
        {
            "capture_id": ctx.capture_id,
            "archive": archive.name,
            "sha256": _sha256_file(archive),
            "unassigned": [{"trace_id": t, "reason": r} for t, r in unassigned],
        },
    )


@always
@task
def cleanup_raw_spool(args, ctx):
    """Remove private raw and sanitized spool copies, on success or failure."""
    local_spool = getattr(ctx, "local_spool", None)
    if local_spool is not None:
        Path(local_spool).unlink(missing_ok=True)
    for temp_attr in ("spool_temp_dir", "sanitized_temp_dir", "partition_temp_dir"):
        temp_dir = getattr(ctx, temp_attr, None)
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return "Removed private spool copies"


def _sanitize_spool(ctx) -> Path:
    """Sanitize the raw spool into a private temporary JSONL file.

    Sanitization happens before any record reaches the artifact directory. A
    final record cut short by a crash is dropped and counted; any other
    malformed line invalidates the capture.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="forge-trace-sanitized-"))
    ctx.sanitized_temp_dir = temp_dir
    sanitized_path = temp_dir / "sanitized-otlp.jsonl"
    ctx.partial_records = 0
    with sanitized_path.open("w", encoding="utf-8") as handle:
        for record in _read_spool_tolerating_partial_tail(ctx):
            handle.write(json.dumps(sanitize_export_request(record)) + "\n")
    return sanitized_path


def _read_spool_tolerating_partial_tail(ctx):
    """Yield records from the raw spool, dropping only an incomplete tail.

    The collector may be killed mid-write; a truncated final line is expected,
    recorded as ``partial_records``. Interior corruption is fatal.
    """
    pending: tuple[int, str] | None = None
    with Path(ctx.local_spool).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if pending is not None:
                yield _decode_spool_line(*pending)
            pending = (line_number, stripped)

    if pending is None:
        return
    try:
        yield _decode_spool_line(*pending)
    except TraceCaptureError:
        # Only the final record may be truncated by a crash or forced stop.
        ctx.partial_records += 1


def _decode_spool_line(line_number: int, line: str) -> dict:
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise TraceCaptureError(f"invalid JSON on spool line {line_number}") from exc


def _iter_span_records_from_file(sanitized_path: Path):
    """Yield span records from the sanitized spool without retaining payloads."""
    yield from iter_span_records(read_archive_records(sanitized_path))


def _partition_spool(ctx, sanitized_path: Path, assignment) -> dict[str, Path]:
    """Partition sanitized records into one private file per benchmark window.

    Enforces ``max_bundle_bytes`` so a capture cannot publish an unbounded
    archive. Records for unassigned traces are not written to any bundle.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="forge-trace-partition-"))
    ctx.partition_temp_dir = temp_dir
    max_bytes = int(getattr(ctx, "max_bundle_bytes", 0) or 0)

    paths = {
        window.test_id: temp_dir / f"{index}.jsonl" for index, window in enumerate(ctx.windows)
    }
    trace_ids = {
        window.test_id: set(assignment.assigned.get(window.test_id, [])) for window in ctx.windows
    }

    sizes = dict.fromkeys(paths, 0)
    handles = {test_id: path.open("w", encoding="utf-8") for test_id, path in paths.items()}
    try:
        for record in read_archive_records(sanitized_path):
            for test_id, handle in handles.items():
                for partitioned in _partition_records([record], trace_ids[test_id]):
                    line = json.dumps(partitioned) + "\n"
                    sizes[test_id] += len(line.encode("utf-8"))
                    if max_bytes and sizes[test_id] > max_bytes:
                        raise TraceCaptureError(
                            f"trace bundle for '{test_id}' exceeds max_bundle_bytes "
                            f"({max_bytes}); reduce sample_ratio or storage sizing"
                        )
                    handle.write(line)
    finally:
        for handle in handles.values():
            handle.close()

    return paths


def _bundle_dir(ctx, window: TraceWindow) -> Path:
    test_dir = Path(window.artifact_dir)
    if not test_dir.is_absolute():
        test_dir = ctx.output_dir / test_dir
    return test_dir / "traces"


def _partition_records(records, trace_ids: set[str]) -> list[dict]:
    """Return records limited to the given trace IDs, preserving OTLP shape."""
    partitioned: list[dict] = []
    for record in records:
        resource_spans_out = []
        for resource_span in record.get("resourceSpans", []) or []:
            scope_spans_out = []
            for scope_span in resource_span.get("scopeSpans", []) or []:
                spans = [
                    span
                    for span in scope_span.get("spans", []) or []
                    if str(span.get("traceId", "")) in trace_ids
                ]
                if spans:
                    scope_span_out = dict(scope_span)
                    scope_span_out["spans"] = spans
                    scope_spans_out.append(scope_span_out)
            if scope_spans_out:
                resource_span_out = dict(resource_span)
                resource_span_out["scopeSpans"] = scope_spans_out
                resource_spans_out.append(resource_span_out)
        if resource_spans_out:
            record_out = dict(record)
            record_out["resourceSpans"] = resource_spans_out
            partitioned.append(record_out)
    return partitioned


def _publish_empty_bundle(ctx, window: TraceWindow, *, reason: str) -> None:
    bundle_dir = _bundle_dir(ctx, window)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    archive = bundle_dir / bundle_mod.ARCHIVE_FILENAME
    with gzip.open(archive, "wt", encoding="utf-8"):
        pass
    summary = _build_summary(
        ctx,
        window,
        status=CollectionStatus.EMPTY,
        reason=reason,
        archive_bytes=archive.stat().st_size,
        archive_sha256=bundle_mod.sha256_of_file(archive),
        quality=CollectionQuality(interrupted=ctx.interrupted),
        observed_services=[],
        trace_count=0,
        span_count=0,
    )
    write_json(bundle_dir / bundle_mod.SUMMARY_FILENAME, summary)


def _publish_bundle(
    ctx,
    window: TraceWindow,
    *,
    records_path: Path | None,
    trace_ids: set[str],
    quality,
    unassigned,
    observed,
    total_trace_count: int,
    total_span_count: int,
    component_error: str = "",
) -> int:
    bundle_dir = _bundle_dir(ctx, window)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    archive = bundle_dir / bundle_mod.ARCHIVE_FILENAME

    span_count = 0
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        if records_path is not None and records_path.is_file():
            for record in _iter_jsonl_file(records_path):
                handle.write(json.dumps(record) + "\n")
                span_count += _count_record_spans(record)

    if component_error:
        status = CollectionStatus.FAILED
        reason = component_error
    elif ctx.interrupted:
        status = CollectionStatus.INCOMPLETE
        reason = "drain interrupted; partial data retained"
    elif span_count == 0:
        status = CollectionStatus.EMPTY
        reason = "no spans assigned to this benchmark window"
    else:
        status = CollectionStatus.COLLECTED
        reason = ""

    summary = _build_summary(
        ctx,
        window,
        status=status,
        reason=reason,
        archive_bytes=archive.stat().st_size,
        archive_sha256=bundle_mod.sha256_of_file(archive),
        quality=quality,
        observed_services=observed,
        trace_count=len(trace_ids),
        span_count=span_count,
        extra={"unassigned_trace_count": len(unassigned)},
        total_trace_count=total_trace_count,
        total_span_count=total_span_count,
    )
    write_json(bundle_dir / bundle_mod.SUMMARY_FILENAME, summary)
    return span_count


def _iter_jsonl_file(path: Path):
    """Yield decoded JSON records from a plain newline-delimited JSON file."""
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise TraceCaptureError(f"invalid JSON on line {line_number}: {path}") from exc


def _count_record_spans(record: dict) -> int:
    return sum(
        1
        for rs in record.get("resourceSpans", [])
        for ss in rs.get("scopeSpans", [])
        for _ in ss.get("spans", [])
    )


def _build_summary(
    ctx,
    window: TraceWindow,
    *,
    status: CollectionStatus,
    reason: str,
    archive_bytes: int,
    archive_sha256: str,
    quality: CollectionQuality,
    observed_services: list[str],
    trace_count: int,
    span_count: int,
    extra: dict | None = None,
    total_trace_count: int | None = None,
    total_span_count: int | None = None,
) -> dict:
    identity_block = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "job_id": ctx.job_id,
        "capture_id": ctx.capture_id,
        "test_id": window.test_id,
        "project": window.project or ctx.project,
        "namespace": ctx.namespace,
        "deployment_id": window.deployment_id or ctx.deployment_id,
    }

    data_block = {
        "archive_relative_path": bundle_mod.ARCHIVE_FILENAME,
        "checksum": {"algorithm": "sha256", "value": archive_sha256},
        "byte_count": archive_bytes,
        "trace_count": trace_count,
        "span_count": span_count,
        "observed_services": observed_services,
    }
    if total_trace_count is not None:
        data_block["capture_trace_count"] = total_trace_count
    if total_span_count is not None:
        data_block["capture_span_count"] = total_span_count
    if extra:
        data_block.update(extra)

    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "identity": identity_block,
        "config": {
            "mode": ctx.mode,
            "sample_ratio": ctx.sample_ratio,
            "collector_image": ctx.collector_image,
            "instrumentation_roles": ctx.instrumentation_roles,
            "sanitization_policy": SANITIZATION_POLICY,
            "sanitization_policy_version": SANITIZATION_POLICY_VERSION,
        },
        "time": {
            "benchmark_window": {"start": window.start, "end": window.end},
            "collection_timestamp": datetime.now(UTC).isoformat(),
            "drain_outcome": "timeout" if ctx.interrupted else "graceful",
        },
        "data": data_block,
        "quality": quality.to_dict(),
        "status": {
            "state": status.value,
            "reason": reason,
        },
    }


@always
@task
def write_capture_result(args, ctx):
    """Write the capture result even when publication failed."""
    if getattr(ctx, "local_spool", None) and ctx.local_spool.exists():
        ctx.local_spool.unlink(missing_ok=True)
    status = getattr(ctx, "status", None)
    result = {
        "capture_id": ctx.capture_id,
        "status": status.value if isinstance(status, CollectionStatus) else "failed",
        "published": getattr(ctx, "published", 0),
    }
    write_json(ctx.output_dir / "trace-capture-result.json", result)
    return "Wrote capture result"


if __name__ == "__main__":
    run.main()
