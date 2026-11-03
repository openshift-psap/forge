# Tracing verification and fixes — 2026-10-28

The five defects reproduced by the final verification have been addressed.
This is local implementation evidence, not release validation of OpenShift,
serving images/controllers, or the deployed MLflow service. Nothing was pushed;
no live cluster, external experiment, or GitHub state was mutated.

## Requirements, implementation, and discriminating tests

| Requirement | Implementation | Evidence |
| --- | --- | --- |
| Sanitize before the first persisted spool | `projects/cluster/toolbox/deploy_trace_collector/templates/{collector-config.yaml,sanitize_otlp.py}`; `projects/cluster/library/tracing/collection.py` | Actual pinned Collector plus Python sidecar: decoded first spool and both log streams exclude synthetic sensitive content; original IDs, timestamps, parent relationships, events, and links survive |
| Reject failed sanitization/forwarding | Synchronous ingress pipeline, no raw file exporter or queue; bounded sanitizer requests/timeouts | `test_tracing_sanitizer_boundary.py`: invalid JSON, oversized/compressed requests, downstream errors and partial acceptance; Collector integration stops sanitizer and verifies HTTP failure with unchanged spool |
| Verify actual writable probe | `deploy_trace_collector/main.py:verify_storage` reads the selected Pod | `test_storage_reads_pod_init_status`: successful, failed and absent exit codes |
| Nested values cannot bypass sanitization | `library/tracing/sanitizer.py`, policy version 2 in `models.py` | Published gzip checked for array, kvlist, and opaque-byte sentinels; original trace ID retained |
| Recover unassigned/conflicting traces | `capture_traces/main.py`, bounded diagnostic archives and checksum manifests outside native bundles | `test_unassigned_diagnostics_survive_cleanup` covers ambiguous, rootless, and outside-window data; conflicts retain both sanitized records before raising |
| Operational report failures are visible | `caliper/orchestration/postprocess.py` notification plus failed step/result | `test_report_failure_notifies_and_marks_result_failed`; raw exception text is excluded from notification |
| Native ownership/replay remains correct | Existing Caliper exporter; local fixture starts one worker with bounded readiness polling | Real SQL-backed MLflow tests for ingestion, single/child ownership, destination conflict, and repeated export without extra child runs |
| Tracing off and profiling remain independent | Existing project adapters and profiler paths retained | Focused configuration, instrumentation, lifecycle, and profiler tests included below |

The initial design assumed Collector processing could apply the full sanitizer.
The previous runtime probe disproved that assumption. The pinned redaction
processor traverses resource and span attributes, but not the event/link policy
needed here ([upstream source](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.115.0/processor/redactionprocessor/processor.go)).
The appended plan amendment records why one shared, standard-library sanitizer
now sits between the public ingress and loopback-only persistence pipelines.
The native file exporter remains the spool writer. A second receiver is not
exposed by the Service. The sanitizer has no spool mount or writable root.

## Commands and observed results

Commands ran in the working checkout with Python 3.12.4 and MLflow 3.12.0,
`PATH=/Users/aperdomo/.pyenv/versions/3.12.4/bin:$PATH` and
`PYTHONDONTWRITEBYTECODE=1`. No installation or source changes were made in the
Benchflow reference checkout.

```sh
python -m pytest -p no:cacheprovider -o addopts= -q \
  projects/cluster/tests/test_tracing*.py \
  projects/caliper/tests/test_tracing*.py \
  projects/llm_d/tests/test_tracing_instrumentation.py \
  projects/rhaiis/tests/test_rhaiis_tracing_instrumentation.py \
  projects/rhaiis/tests/test_profiler_trace_transfer.py \
  projects/rhaiis/tests/test_profiler_traces.py
```

Observed: **168 passed, 1 skipped, 10 setup errors** in 142.19s. The skip was the
opt-in container test. All ten errors were the same disposable MLflow fixture
cold-start deadline; the server log showed successful startup near the deadline.
The fixture now uses a 180-second monotonic deadline, closes health responses,
and bypasses proxies for loopback readiness. The targeted rerun was:

```sh
python -m pytest -p no:cacheprovider -o addopts= -q \
  projects/caliper/tests/test_tracing_mlflow_ingest.py \
  projects/caliper/tests/test_tracing_multi_run_export.py
```

Observed: **10 passed in 19.53s**. These tests use a new local SQLite database;
no external MLflow data is read or modified. A pytest-asyncio fixture-scope
configuration deprecation warning remains unrelated to tracing behavior.

```sh
podman pull ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.115.1
podman pull python:3.12.8-slim-bookworm
FORGE_TEST_COLLECTOR=1 python -m pytest -p no:cacheprovider -o addopts= -q \
  projects/cluster/tests/test_tracing_collector_integration.py
```

Final run: **2 passed in 38.32s**, covering multi-service sanitization, restart,
sanitizer failure, and ingress rejection when a 64 KiB spool fills. An earlier
fixture attempt failed because Podman reassigned its published port on restart;
the fixture now resolves the port again. Containers and volumes are removed in
`finally`. The fixture requires cached images and fails explicitly if missing.

```sh
ruff check projects/ bin/
ruff format --check projects/ bin/
git diff --check
```

Observed: lint passed, **463 Python files** already formatted, no whitespace errors.

## Remaining release gates and how to close them

1. **OpenShift storage/admission:** select an authorized test cluster, namespace,
   storage class, and image pull policy. Run `deploy_trace_collector` with a
   unique capture identity, verify the Pod's init-container exit status, and
   exercise collector container restart and whole-Pod replacement against its
   PVC. Check arbitrary UID access for all three containers and recovery after
   volume exhaustion. Local restart/ENOSPC tests cannot establish SCC behavior.
2. **Producer coverage:** select exact vLLM and controller images, then run each
   project's `tracing-smoke` preset at full sampling with two sequential workloads
   and warmup. Verify original IDs across component boundaries, workload-specific
   bundles, failure-time capture, and late spans. EPP and P/D are still not enabled;
   their controller-specific adapters and propagation smoke evidence remain work
   before claiming full topology support. SGLang/TRT-LLM remain rejected.
3. **Deployed MLflow:** select an explicitly authorized validation workspace and
   experiment. Replay sanitized synthetic bundles through the existing Caliper
   export entrypoint, checking proxy authentication, URI prefixes, experiment and
   workspace routing, lookup/link permissions, payload limits, timeout-after-acceptance,
   duplicate POSTs, and duplicate linkage. Local SQLite behavior is insufficient
   evidence for that deployed destination.
4. **Drain and performance:** run the same deployment/workload/seed in off,
   standard, and detailed modes. Record throughput, latency, sampling, producer
   flush timing, Collector/sanitizer CPU and memory, and trace bytes. Measure the
   new synchronous sanitizer hop before recommending performance-test defaults.
   A quiet spool continues to mean observed settling, not proof of flushed
   producer buffers.

These gates require explicit validation targets; none was supplied or used.
The implementation does not claim full tracing support or release validation.
