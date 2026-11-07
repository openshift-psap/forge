# FORGE distributed tracing — operating guide

FORGE collects distributed request traces from serving workloads, keeps them as
ordinary test artifacts, and (optionally) ingests them into MLflow's native
tracing endpoint so each trace is attached to the same experiment, workspace,
and run as the benchmark that produced it.

**Tracing is off by default.** An untraced run creates no collector, injects no
arguments, needs no tracing dependencies, and never contacts the native trace
endpoint.

## Architecture (artifact-first)

```text
serving pods --OTLP--> OTel Collector --native file exporter--> sanitized
                       (per-capture, on a PVC)                   OTLP JSONL spool
                                                                    |
                        capture toolbox drains/transfers/partitions per test
                                                                    |
                        <test dir>/traces/otlp.jsonl.gz + trace-summary.json
                                                                    |
                        Caliper export: encode OTLP protobuf -> POST /v1/traces ->
                        link traces to the owning (child) MLflow run -> receipt
```

Traces are collected to artifacts first and exported during the existing Caliper
export phase. This fits FORGE's child runs (created during export) and makes a
failed upload recoverable without repeating the benchmark.

## Configuration

A common top-level `tracing` section enables collection, and a Caliper switch
enables native export:

```yaml
tracing:
  mode: "off"          # "off" | standard | detailed (quote off for YAML 1.1)
  sample_ratio: 0.1    # 0.0 - 1.0, parent-based trace-ID-ratio sampling
  collector:
    image: ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.115.1
    storage_size: 10Gi
    storage_class: null
    ready_timeout_seconds: 300
    settle_timeout_seconds: 300
    max_bundle_bytes: 2147483648

caliper:
  export:
    traces:
      enabled: true    # ingest discovered bundles natively when MLflow export runs
```

For projects using `config.d/tracing.yaml`, the file contents are the `tracing`
section (starting with `mode`). Enable tracing with an ordinary preset, for
example:

```yaml
tracing-smoke:
  extends:
    - smoke
  tracing.mode: standard
  tracing.sample_ratio: 1.0
```

Keep artifact collection usable without an MLflow connection. Disabling native
trace export does not remove trace artifacts. Enabling native export without any
bundle produces a clear *skipped* result, not an empty request.

## Supported producers

| Project | Engine | Topology | Status |
| --- | --- | --- | --- |
| llm_d | vLLM | Standard serving, with an explicit EPP template when a scheduler is present | instrumented; needs OpenShift smoke validation |
| llm_d | vLLM | Controller-default scheduler (`scheduler: {}`) | **rejected** with tracing; supply a complete scheduler argument template |
| rhaiis | vLLM | ServingRuntime (single pod) | instrumented; needs OpenShift smoke validation |
| llm_d | vLLM | P/D (prefill/decode) | **rejected** (helpers exist; not validated end-to-end) |
| llm_d | vLLM | routing proxy | **rejected** (helpers exist; not validated end-to-end) |
| rhaiis | SGLang / TRT-LLM | any | **rejected** until a capability check + smoke test exist |

For llm_d, tracing instruments the standard vLLM serving container and the EPP
scheduler's `main` container. EPP receives `--tracing=true`, its own deployment
service name, FORGE capture attributes, the collector's gRPC endpoint, and the
same parent-based sampling settings as vLLM. Existing scheduler configuration,
arguments, environment, images, and other containers are preserved. Standard and
detailed modes both collect routing spans; detailed model-phase flags apply only
to vLLM. No fixed request-wide `traceparent` is injected.

Use the approximate-prefix-cache or precise-prefix-cache deployment profiles,
which provide complete EPP arguments. KServe merges container environment by
name but replaces argument lists. Adding only `--tracing=true` to a
controller-default scheduler would discard its routing arguments. FORGE therefore
rejects tracing for `scheduler: {}` or a scheduler template without explicit
`main` arguments. A simple deployment with no scheduler still supports vLLM
tracing. See the [controller merge implementation](https://github.com/kserve/kserve/blob/master/pkg/controller/v1alpha2/llmisvc/config_merge.go)
and [EPP tracing configuration](https://github.com/llm-d/llm-d-router/blob/main/config/charts/routerlib/templates/_deployment.yaml).

At full sampling, each workload must contain both expected service names and
an EPP parent span for a vLLM span in the same trace. Missing components or
missing propagation fail collection, retain sanitized bundles, and trigger the
orchestration failure notification.
Observed services are scoped to each workload's bundle. At lower sampling,
partial component visibility can be expected and this acceptance gate is skipped.
These artifact checks do not establish that an arbitrary serving image emits
spans; run `tracing-smoke` on the selected images and controller to validate them.

P/D and routing-proxy integration are outside this change and remain rejected.

Requesting tracing for an unsupported engine or topology raises before anything
is rendered or applied. There is no silent fallback.

## Adapter contract (other projects)

A project adopting tracing implements a small adapter and calls shared code
only:

1. Resolve shared settings (`projects/cluster/library/tracing/models.py`,
   `settings_from_config`).
2. Instrument supported containers with the pure helpers in
   `projects/cluster/library/tracing/instrumentation.py`.
3. Build a capture identity (`CaptureIdentity`) and start a capture before the
   instrumented pods (`projects/cluster/library/tracing/lifecycle.py`,
   `start_capture`).
4. Record each benchmark window (`lifecycle.record_window`) into the workload's
   own artifact subtree. Keep warmup/profiler windows separate.
5. Capture, then clean up (`lifecycle.capture` / `lifecycle.cleanup`) before
   serving resources are removed.

No project adapter imports another project's orchestration, and toolbox commands
receive only explicit typed inputs.

## Artifact layout

```text
<test directory>/
  __caliper_test_metadata__.yaml
  <numbered capture command>/
    artifacts/
      traces/
        otlp.jsonl.gz          # sanitized native OTLP archive
        trace-summary.json     # identity, config, time, data, quality, status
        mlflow-export.json     # resumable native-export receipt
  reports/
    trace_distribution_report.html
    trace_report.json
```

The archive is sanitized **before** it is written: prompts, completions,
request/response bodies, headers, cookies, URL credentials/query strings, and
arbitrary exception text never reach the spool, archives, reports, or the native
endpoint.

## Recovery and cleanup

- The collector and spool are **always removed** after capture, whatever its
  outcome, and also when the test failed before any benchmark window was
  recorded. A failed capture still fails requested tracing and writes a
  notification; any published bundles and diagnostics are already in the
  artifacts. Resources left by an interrupted run can be removed explicitly:

  `./bin/run_toolbox cluster cleanup_trace_collector --namespace NS --capture-id CAP --force`

- Repeated export resumes the existing child runs and skips already-ingested
  traces via the scope-checked receipt. A receipt scoped to a different tracking
  server, workspace, experiment, run, or archive is rejected, never silently
  reused.
- If ingestion succeeded but run linkage failed, only linkage is retried.
- `collector.settle_timeout_seconds` bounds the post-benchmark drain. When it
  expires the valid partial data is still published, but the bundle is marked
  `incomplete` with `time.drain_outcome: timeout`, requested tracing fails, a
  notification is written.
- A benchmark window whose bundle holds no assigned spans is recorded `empty`,
  and any such window makes the run's status `empty`. That is accepted at low
  sampling (an expected outcome), but at `sample_ratio: 1.0` it fails requested
  tracing with a `tracing-collection-empty-at-full-sampling` notification: it
  means instrumentation or the collector endpoint is misconfigured.
- `collector.max_bundle_bytes` bounds each published bundle. Exceeding it fails
  the capture before a bundle is written, so reduce `sample_ratio` or raise the
  budget deliberately.
- A final spool line truncated by a crash is dropped and counted in
  `quality.partial_records`; corruption anywhere else fails the capture.
- The spool transfer verifies both the copied byte count and the remote sha256
  (when the reader image can compute it) before any archive is published.

## Operator checklist for an untraced run

- `tracing.mode: "off"` (the default; quote it in YAML) — no collector, no injected flags, no
  tracing dependencies, no native request.
- `caliper.export.traces.enabled: false` — artifacts still upload; native export
  is reported skipped.

## Release gates (evidence still required)

These capabilities are implemented and covered by local fixtures, but are **not**
release-validated until the evidence below exists. Live clusters and external
MLflow are not mutated by this repository's tests.

| Decision | Evidence required |
| --- | --- |
| Collector image and file contract | Pinned-image replay, restart, flush, and storage tests on OpenShift |
| Client/server baseline | Actual native endpoint, auth proxy, workspace routing, run linkage, duplicate-ingestion tests |
| Storage defaults and retention | Expected trace volumes and tested OpenShift storage behavior |
| Supported producer matrix | Image/controller-specific standard vLLM and EPP smoke results |
| Drain deadline and quality interpretation | Producer batching measurements and interrupted-run tests |
| Sanitization allowlist | Safe diagnostic-field review and sentinel tests for every persisted surface |
| Performance guidance | Controlled overhead measurements, tracing off vs standard vs detailed |
| EPP validation | Standard smoke runs proving both component spans and context propagation |

## Measured compatibility evidence (Stage 1)

Local, disposable fixtures established on MLflow 3.12.0 with a SQL-backed
store:

- OTLP protobuf is accepted at `POST /v1/traces` (header `x-mlflow-experiment-id`
  required; `X-MLFLOW-WORKSPACE` for workspace routing).
- The native trace ID is `tr-<otel-trace-hex>` and resolves through
  `client.get_trace(...)`.
- The server drops resource attributes and keeps span attributes, so FORGE
  identity is copied into `forge.*` **span** attributes in the export payload
  only; the archived source record is never mutated.
- `client.link_traces_to_run(...)` links at most 100 traces per call.
- Duplicate ingestion of the same payload is idempotent on this baseline
  (3.12 + SQLite); exactly-once delivery is not claimed on the strength of a
  local checkpoint alone.

## Sanitization boundary and diagnostic recovery

The first Collector receiver accepts gRPC/HTTP OTLP and synchronously forwards
OTLP JSON to a loopback-only, standard-library Python sanitizer sidecar. The
sidecar imports the same versioned policy as artifact capture, sanitizes in
memory, and forwards to a second loopback-only Collector receiver connected to
the native file exporter. The raw pipeline has no batch processor, persistent
queue, or file exporter. Sanitizer errors reject ingress; there is no bypass.
The sidecar has no spool mount, a read-only root filesystem, a 16 MiB request
limit, socket/forwarding deadlines, and bounded CPU/memory. Source files are
mounted from the capture-owned ConfigMap; no runtime package installation is
needed. Its image is pinned to `python:3.12.8-slim-bookworm`.

Policy version 2 recursively filters key/value lists and redacts arrays, removes
opaque bytes and tracestate, and reviews scope identity and schema URLs.
The policy runs again during capture as defense in depth. It preserves original
trace/span IDs, hierarchy, timestamps, events, and link identities.

Unassigned traces are retained in `trace-diagnostics/unassigned.otlp.jsonl.gz`
at the capture root, with per-trace IDs/reasons and a checksum in
`attribution.json`. These files are ordinary diagnostics, not discoverable
native-export bundles. Missing roots and ambiguous ownership fail collection
at every sampling ratio. Outside-window traces
(such as warmup) remain diagnostic-only without failing a valid measured window.
Conflicting captures retain `invalid.otlp.jsonl.gz` and `validation.json` before
raising. The configured bundle size limit also bounds diagnostic archives.
Report errors now create `tracing-report-failed`, mark postprocessing failed,
and leave retained artifacts available for independent export/replay.

## Reproduce local boundary validation

The container test uses only disposable local containers and a volume. Pre-pull
these images using Podman:

```sh
podman pull ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.115.1
podman pull python:3.12.8-slim-bookworm
FORGE_TEST_COLLECTOR=1 python -m pytest projects/cluster/tests/test_tracing_collector_integration.py
python -m pytest projects/cluster/tests/test_tracing_regressions.py projects/cluster/tests/test_tracing_sanitizer_boundary.py
python -m pytest projects/caliper/tests/test_tracing_mlflow_ingest.py projects/caliper/tests/test_tracing_multi_run_export.py
```

The Collector fixture tests the first persisted record, link identity, nested
sentinels, sanitizer failure, append after restart, and rejection on a full spool. The MLflow fixture starts
one loopback worker on a disposable SQLite store and scrubs live credentials.
These tests do not establish OpenShift SCC/storage behavior, deployed
workspace/auth-proxy compatibility, producer span emission, or tracing overhead.
EPP is instrumented through explicit scheduler templates. Its image/controller
compatibility and propagation must be proven on an explicitly authorized test
cluster. P/D integration is deferred to a separate change.
