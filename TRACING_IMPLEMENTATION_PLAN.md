# FORGE distributed tracing implementation plan

Date: 2026-10-27

Status: Proposed implementation plan

FORGE should collect distributed request traces from its serving workloads, retain them as ordinary test artifacts, and ingest them into MLflow's native tracing endpoint. Each trace must be associated with the same experiment, workspace, and test run as the benchmark that produced it.

Implement collection and instrumentation helpers once, then add small adapters for each project. Start with vLLM in `llm_d` and `rhaiis`, including EPP and the P/D routing proxy where the deployed components support tracing. Caliper should own reporting and MLflow ingestion so other projects can reuse those capabilities without implementing another exporter.

The proposed flow collects artifacts first and exports native traces during the existing Caliper export phase. This accommodates FORGE's child runs, which are created during export, and makes failed uploads recoverable without repeating a benchmark. Native traces become visible after export; live MLflow streaming is outside the initial scope.

## Outcomes and scope

The implementation is complete when a traced benchmark produces all of the following:

- A sanitized, replayable OpenTelemetry Protocol (OTLP) archive below its Caliper test directory.
- A summary describing the capture window, sampling, observed components, counts, and collection quality.
- Native MLflow traces with preserved trace IDs, span IDs, parent relationships, and timing.
- Explicit linkage to the owning single run or child run.
- A shared Caliper report for trace distributions and capture quality.
- Visible failure status and a notification when requested collection or ingestion fails.

Tracing remains off by default. An untraced run must not create a collector, alter serving arguments, require tracing dependencies, or contact the native trace endpoint.

This work covers distributed request traces. Existing rhaiis PyTorch profiler files describe GPU and execution activity in a different format. Keep their collection and S3 behavior separate; do not submit Chrome or PyTorch trace JSON to the OTLP endpoint or silently convert those files into request spans.

The first release supports managed deployments with known instrumentation paths. Arbitrary external endpoints, simultaneous benchmarks against one reused deployment without request correlation, and unvalidated serving engines must be rejected when tracing is requested. They must not appear to work through a silent fallback.

## Existing behavior and reusable foundations

### Benchflow

The reference repository is the sibling checkout at `../benchflow`.

| Source | Behavior to reuse or adapt |
| --- | --- |
| [`src/benchflow/tracing.py`](../benchflow/src/benchflow/tracing.py) | Tracing modes, sampling, service identities, vLLM flags, collection summaries, and span normalization |
| [`src/benchflow/deploy/llmd.py`](../benchflow/src/benchflow/deploy/llmd.py) | Separate instrumentation of vLLM, EPP, and the P/D routing proxy |
| [`src/benchflow/renderers/deployment.py`](../benchflow/src/benchflow/renderers/deployment.py) | RHOAI instrumentation through explicit serving and scheduler configuration |
| [`src/benchflow/assets/setup/llmd/tracing.yaml`](../benchflow/src/benchflow/assets/setup/llmd/tracing.yaml) | Collector deployment and OTLP receivers |
| [`src/benchflow/artifacts.py`](../benchflow/src/benchflow/artifacts.py) | Collection scoped to benchmark timestamps |
| [`src/benchflow/reports/tracing.py`](../benchflow/src/benchflow/reports/tracing.py) | Distribution reports with metric units and provenance |
| [`src/benchflow/mlflow_upload.py`](../benchflow/src/benchflow/mlflow_upload.py) | Upload of the resulting files as ordinary MLflow artifacts |

Benchflow currently uses Collector → Jaeger → post-benchmark queries. It scopes queries by release-specific service names and time, deduplicates traces, and writes compressed normalized spans. Its tracing artifacts are uploaded with the benchmark artifacts; the inspected uploader does not perform native MLflow trace ingestion.

Reuse the instrumentation knowledge and reporting concepts, while adapting the operational implementation to FORGE's DSL. In particular, do not carry over fixed flush sleeps, catch-and-warn collection failures, namespace-global resource ownership, or a filter that drops every span named `GET`. A trace containing multiple services is not necessarily a complete tree.

### FORGE

| Existing component | Integration responsibility |
| --- | --- |
| [`projects/core/dsl/`](projects/core/dsl/) | Task ordering, retries, failure capture, and per-command artifacts |
| [`projects/core/library/config.py`](projects/core/library/config.py) | Typed YAML, presets, and overrides |
| [`projects/core/library/vault.py`](projects/core/library/vault.py) | File-based access to credentials |
| [`projects/core/library/postprocess.py`](projects/core/library/postprocess.py) | Test metadata, timing, completion, and post-processing lifecycle |
| [`projects/cluster/library/prom/collection.py`](projects/cluster/library/prom/collection.py) | Existing precedent for shared observability orchestration |
| [`projects/caliper/orchestration/export.py`](projects/caliper/orchestration/export.py) | Destination resolution and single-run versus multi-run export |
| [`projects/caliper/engine/file_export/mlflow_backend.py`](projects/caliper/engine/file_export/mlflow_backend.py) | Creation or resumption of the run that will own exported traces |
| [`projects/caliper/engine/file_export/mlflow_secrets.py`](projects/caliper/engine/file_export/mlflow_secrets.py) | Connection validation, credential loading, and scoped environment handling |
| [`projects/llm_d/orchestration/test_phase.py`](projects/llm_d/orchestration/test_phase.py) | Model/profile/workload execution and finalizers |
| [`projects/rhaiis/orchestration/test_phase.py`](projects/rhaiis/orchestration/test_phase.py) | Multiple workload benchmarks on a serving deployment |

The job-level `_mlflow_destination.yaml` already records a run ID, experiment ID, and workspace. Multi-run export creates nested child runs later. Do not make the collector assume the job-level run is the final destination for every trace.

## Architecture

```text
Project orchestration
  resolve tracing settings and deployment identity
  deploy collector and instrument serving components
  record benchmark windows and execute workloads
          |
          v
vLLM / EPP / routing proxy
          | OTLP
          v
Collector
  validate scope -> sanitize -> batch -> native OTLP spool
          |
          v
Capture toolbox
  drain and snapshot -> verify transfer -> partition by test
          |
          v
Caliper test artifacts
  OTLP archive + collection summary
          |
          +--> Shared trace report
          |
          v
Caliper export resolves actual MLflow destination
          |
          +--> Ordinary artifact upload
          |
          +--> POST /v1/traces -> link traces to owning run
                                  |
                                  v
                           Export receipt and status
```

Prefer the Collector's native file exporter for the spool. Native OTLP preserves resource and instrumentation-scope data, span events and links, and nanosecond timestamps without reconstructing them from Jaeger responses. It also removes a second tracing service and query-pagination logic from the initial deployment.

The file exporter supports newline-delimited OTLP JSON, but its tracing stability is marked alpha. Pin a collector image and validate its exact output and shutdown behavior in the first implementation stage. The exporter requires writable storage and does not provide an end-to-end durability guarantee by itself. These are release gates, not reasons to hide failures or maintain an automatic Jaeger fallback. See the [OpenTelemetry file exporter documentation](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/fileexporter/README.md).

## Configuration contract

Use a common top-level `tracing` section across participating projects and a native-export switch in Caliper:

```yaml
tracing:
  mode: off
  sample_ratio: 0.1

caliper:
  export:
    traces:
      enabled: true
```

This is proposed configuration, not an existing supported interface. Add the corresponding typed validation and export model fields during implementation.

| Setting | Meaning |
| --- | --- |
| `tracing.mode: off` | No collection resources, injected flags, or tracing environment |
| `tracing.mode: standard` | Ordinary distributed request spans |
| `tracing.mode: detailed` | Ordinary spans plus supported vLLM detailed model-phase tracing |
| `tracing.sample_ratio` | Numeric value from 0 through 1 for parent-based trace-ID-ratio sampling |
| `caliper.export.traces.enabled` | Ingest discovered trace bundles natively when MLflow export is enabled |

Keep artifact collection usable without an MLflow connection. Disabling native trace export does not remove trace artifacts. Enabling native export without a trace bundle should produce a clear skipped result rather than an empty request.

Define collector image, storage size/class, resource requests/limits, and bounded capture timeouts in one shared configuration model. Finalize their operational defaults from the collector proof. Avoid introducing duplicate enabled flags, per-project copies of defaults, or arbitrary exporter selection.

For projects using `config.d/tracing.yaml`, the file contents are the contents of the `tracing` section, starting with `mode`, rather than an additional nested `tracing:` key. Load it through existing `config.init()` behavior. Use ordinary presets to enable tracing; do not add new preset inheritance machinery.

Validate configuration before applying resources. Reject unknown modes, invalid YAML types, unsupported engine/topology combinations, conflicting manually supplied tracing flags, and incompatible reuse requests. Do not coerce malformed YAML values into accepted types.

## Shared code and toolbox boundaries

The following are proposed new modules. Existing source paths above remain the integration anchors.

| Proposed location | Responsibility |
| --- | --- |
| `projects/cluster/library/tracing/models.py` | Settings, capture identity, bundle summary, and collection result types |
| `projects/cluster/library/tracing/instrumentation.py` | Pure helpers for OTLP arguments, environment, and component identity |
| `projects/cluster/library/tracing/collection.py` | Compose collector lifecycle commands with explicit inputs |
| `projects/cluster/toolbox/deploy_trace_collector/` | Apply collector resources and wait for readiness |
| `projects/cluster/toolbox/capture_traces/` | Snapshot, transfer, validate, partition, and publish trace bundles |
| `projects/cluster/toolbox/cleanup_trace_collector/` | Remove resources owned by a capture after recovery conditions are checked |
| `projects/caliper/engine/tracing/` | Bundle validation, trace analysis, OTLP encoding, and MLflow ingestion |
| `projects/caliper/postprocess/helpers/` | Shared trace report generation using Caliper's plotting conventions |

Toolbox entrypoints receive typed parameters such as namespace, collector name, capture ID, output path, time bounds, and timeouts. A persisted manifest file is appropriate for multiple benchmark windows; a project configuration blob is not. Templates and static helper scripts live beside the command that owns them.

Only orchestration resolves project configuration. Shared helpers and toolbox commands must not import `projects.<name>.orchestration`. Pure instrumentation helpers accept concrete containers or engine arguments and return concrete changes. They must not initialize config, access vaults, or apply manifests.

Use `@retry` for readiness and bounded observation, and `@always` where diagnostics must survive ordinary task failure. Keep networking, parsing, and transport errors typed. Do not place trace contents, credentials, or authentication headers in task arguments or shared context: the DSL persists both.

## Collector storage and lifecycle

### Resource ownership

Create a collector instance scoped to the FORGE job and managed deployment lifecycle, with generated names and ownership labels. Sequential workloads can reuse that instance. Separate jobs must not share a mutable collector configuration or delete each other's resources.

Use ordinary Deployment, Service, ConfigMap, and storage resources. The initial implementation should not require an OpenTelemetry operator. Apply only non-secret resources through `oc_apply()`.

Serving components receive a cluster-internal endpoint and non-secret tracing identity. No MLflow credentials are required by the collector or serving pods. Support the existing gRPC producers and HTTP producers through explicit receiver configuration.

### Spool storage

Use a PVC for recovery across collector pod replacement. Validate storage availability and OpenShift UID/volume permissions before starting the benchmark. Set resource and storage bounds so tracing cannot consume the serving workload's resources without limit.

Configure append/restart behavior deliberately and test that restarting the collector does not truncate existing data. Avoid rotation policies that silently discard uncollected segments. Storage exhaustion or exporter write failure makes collection incomplete and fails the requested tracing operation.

Use a small artifact-reader container with read-only access to the spool for copying and checksums. Do not assume the collector image contains `tar`, a shell, or Python. Reuse FORGE's Kubernetes transport helpers where suitable and verify copied byte counts and hashes before publishing a bundle.

The target-cluster spool is separate from the Fournos artifact workspace. Collection must work even when the execution pod and serving workload are on different clusters; it cannot assume they can mount the same PVC.

### Benchmark lifecycle

1. Resolve a stable capture identity and create the collector before instrumented serving pods start.
2. Verify collector readiness, writable storage, and supported producer instrumentation.
3. Record the actual benchmark start and end in UTC. Keep smoke, warmup, profiling, and measured benchmark windows distinct.
4. After the benchmark, poll for bounded settling using observed data and collector counters. Account for producer batching; do not substitute a fixed sleep.
5. Snapshot only complete OTLP records, transfer them, validate them, and publish test-scoped bundles atomically.
6. Reconcile late spans before closing the deployment capture. Where graceful producer shutdown is available, drain producers while the collector remains alive, then take the final collector snapshot.
7. Delete the collector and spool only after verified capture. On capture failure, preserve recovery information and avoid automatically deleting the only remaining trace copy. An explicit cleanup command can remove retained resources later.

A stable file size or quiet collector does not prove all producer buffers were delivered. Record the drain method, timeout, observed exporter failures, collector restarts, and whether the capture was interrupted. Forced pod loss or process termination can leave an incomplete capture even when the surviving file is valid.

Persist capture identity and benchmark windows early enough for a later finalization phase to recover available data after a test-process interruption. Ordinary `finally` blocks and `@always` cannot guarantee execution after SIGKILL or loss of the execution pod.

## Trace identity and workload attribution

Use three explicit identities:

- **Job identity:** one FORGE job or local invocation.
- **Deployment capture identity:** the lifetime of an instrumented serving deployment and collector.
- **Test identity:** the particular model/profile/workload combination represented by a Caliper test node.

Set `service.name` to a unique deployment-scoped name plus a component role. Add reviewed non-secret resource attributes for job, deployment capture, project, and component role. Keep the deployment identity stable while a reused deployment runs multiple workloads.

Initially support sequential workload windows for a reused deployment. Assign a trace by its root request's start time and capture scope, then retain all of that trace's observed spans, including children arriving after the benchmark end. Time padding is for collection, not permission to assign a trace to multiple neighboring tests.

Do not derive ownership from truncated Kubernetes names alone. Use explicit IDs in metadata. Do not assign overlapping or ambiguous traces to a convenient test. Record them as unassigned with a reason, retain their sanitized data in the capture diagnostics, and surface the attribution failure.

If the root is missing, record the trace as structurally incomplete. Do not synthesize a root or invent parent relationships to make the UI look complete. Later support for concurrent workloads sharing a deployment requires validated request-level correlation and context propagation.

Deduplicate by original trace ID and span ID. Treat conflicting duplicate payloads as a validation failure. Preserve source timestamps rather than replacing them with collection or upload times.

## Artifact contract

Place each bundle below its owning `__caliper_test_metadata__.yaml` test node. The logical layout is:

```text
<test directory>/
  __caliper_test_metadata__.yaml
  <numbered capture command>/
    _meta/
    task.log
    artifacts/
      traces/
        otlp.jsonl.gz
        trace-summary.json
        mlflow-export.json
  reports/
    trace_distribution_report.html
```

The export receipt and report are created later. Discover bundles through versioned summaries and validated relative paths, rather than relying on a fixed numbered command directory. Assign each bundle to its nearest owning Caliper test node, and prevent parent traversal, symlink escape, or duplicate discovery.

The archive contains sanitized native OTLP batches. Compress with gzip after validation; do not assume the collector's own compressed output is a standard gzip stream. Keep capture processing bounded for large runs, using streaming reads and a disk-backed trace index where necessary rather than loading the entire archive into memory.

The summary schema must include:

| Field group | Required information |
| --- | --- |
| Identity | Schema version, job ID, capture ID, test ID, project, namespace, and deployment ID |
| Configuration | Tracing mode, sample ratio, collector image, instrumentation roles, sanitization-policy version |
| Time | Benchmark window, capture bounds, collection timestamp, and drain outcome |
| Data | Relative archive path, checksum, byte count, trace count, span count, observed services |
| Quality | Rootless traces, missing-parent spans, multi-service traces, duplicates, unassigned traces, transport/exporter errors |
| Status | `collected`, `empty`, `incomplete`, or `failed`, with a non-sensitive reason |

Keep structural validation separate from delivery completeness. Even a well-formed tree does not establish that all instrumentation emitted every expected span. A single-service trace is valid for rhaiis; multiple services alone do not establish completeness for llm_d.

An empty capture can be expected with zero sampling or a small sampled workload. Record that outcome explicitly. A smoke test with sampling at 1.0 and confirmed requests must fail acceptance if no expected service spans arrive.

## Instrumentation and project integration

### Shared instrumentation

Adapt Benchflow's settings for each supported producer:

- A unique `OTEL_SERVICE_NAME` and reviewed `OTEL_RESOURCE_ATTRIBUTES`.
- The collector endpoint and explicit OTLP exporter/protocol configuration where required.
- `parentbased_traceidratio` sampling with the configured ratio.
- vLLM `--otlp-traces-endpoint`, with `--collect-detailed-traces=all` only in detailed mode on supported versions.
- EPP tracing enablement appropriate to the controller and scheduler template.
- P/D routing-proxy `--tracing=true` and its exporter environment, independently of EPP.

Validate producer versions and dependency availability. Successful manifest rendering is not proof that a container supports the flags or emits spans. Verify context propagation across component boundaries in the smoke tests. Do not assign a single traceparent to every benchmark request.

### llm_d

Integrate into [`render_inference_service.py`](projects/llm_d/orchestration/render_inference_service.py), [`runtime_config.py`](projects/llm_d/orchestration/runtime_config.py), and [`test_phase.py`](projects/llm_d/orchestration/test_phase.py).

1. Resolve tracing settings and deployment identity before rendering the LLMInferenceService.
2. Apply shared helpers to standard serving containers and both prefill and decode templates. Handle the existing `VLLM_ADDITIONAL_ARGS` construction explicitly.
3. Configure scheduler/EPP and routing-proxy templates through supported controller fields. Verify the resulting pods, including controller-injected sidecars. Do not patch generated pods as a substitute for declarative configuration.
4. Record each `RunSpec` benchmark window and collect into that run's artifact subtree.
5. Add trace capture to finalization alongside service state, events, and Prometheus collection. Preserve primary test exceptions while making trace failures visible.
6. Reconcile traces before deleting collector storage and before the final post-processing/export pass.
7. When resource reuse is requested, verify that the existing deployment has the required endpoint, identity, and instrumentation. Reject incompatible reuse rather than claiming collection succeeded.

### rhaiis

Integrate into [`manifests.py`](projects/rhaiis/orchestration/manifests.py) and [`test_phase.py`](projects/rhaiis/orchestration/test_phase.py).

1. Start with the vLLM `ServingRuntime` builder, which already accepts engine arguments and environment values.
2. Create a deployment capture before applying the ServingRuntime and InferenceService.
3. Record benchmark timing around each `_run_workload_benchmark()` invocation, including exceptions, and publish a bundle below that workload's existing test node.
4. Keep warmup/profiler windows separate. The current deployment can serve several workloads, so deployment scope alone is insufficient for attribution.
5. Capture on benchmark failure and perform final reconciliation before `_capture_and_cleanup()` removes serving resources and before Caliper processes the final artifacts.
6. Leave existing profiler trace transfer and dashboard/S3 integration intact.

SGLang and TRT-LLM require separate instrumentation capability checks and smoke tests. Until those adapters exist, reject tracing requests for those engines explicitly.

### Other projects

The adoption contract is small: resolve shared settings, instrument supported containers, record test windows, and invoke shared lifecycle functions. Any project producing a valid bundle receives Caliper reporting and native export without project-specific MLflow code.

Do not implement an implicit scan-and-mutate hook across all project manifests. Different controllers own different pod templates and sidecars; each adapter must identify the containers it can safely instrument.

## Native MLflow ingestion

### Server and dependency requirements

MLflow documents OTLP ingestion at `POST /v1/traces` from version 3.6 onward, backed by a SQL store. It accepts OTLP over HTTP; do not direct a gRPC producer to the tracking server. Requests select an experiment with `x-mlflow-experiment-id` and, for workspace deployments, `X-MLFLOW-WORKSPACE`. See [MLflow OTLP ingestion](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/ingest/).

FORGE's current `mlflow>=2.10.0` optional dependency is insufficient to guarantee this feature. The reviewed local client is 3.12.0. Stage 1 must select a tested client/server baseline, verify the actual deployed server and authentication proxy, and update the relevant dependency declarations. Keep optional tracing imports lazy so disabled tracing does not break unrelated commands.

Check native ingestion, workspace routing, trace lookup, run linking, payload limits, and required permissions together. An available artifact-upload endpoint is not proof that the trace endpoint is usable.

### Export sequence

Extend `log_artifacts()` and `log_multi_run_artifacts()` through a shared exporter rather than writing project-specific upload code:

1. Discover and validate bundles using the same test-directory discovery as Caliper export.
2. Resolve or resume the destination run through the existing export machinery.
3. Persist the association between stable test identity, relative directory, and actual run ID before native ingestion begins.
4. Read the sanitized archive and encode bounded OTLP protobuf requests. Preserve IDs, timing, kinds, events, links, and attributes. Group by trace when practical; handle an oversized trace explicitly instead of silently dropping it.
5. Send to the configured tracking URI's `/v1/traces`, preserving any supported URI prefix, using vault-derived authentication and existing TLS policy.
6. Validate acceptance and resolve the MLflow trace identifiers using the tested server contract. Do not invent new IDs or rely on an untested internal ID mapping.
7. Link accepted traces to the owning run in batches of at most 100 using `MlflowClient.link_traces_to_run`. See the [MLflow client API](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.client.html#mlflow.client.MlflowClient.link_traces_to_run).
8. Write and upload the receipt, and incorporate trace ingestion/linking into Caliper export status and notifications.

OTLP JSON uses hexadecimal trace/span IDs and string representations for 64-bit timestamps. A generic protobuf JSON parser expects different encoding for byte fields. Test conversion against the [OTLP specification](https://opentelemetry.io/docs/specs/otlp/) rather than passing the archive through an unmodified generic parser.

Preserve all reviewed source metadata in the archive. Verify which resource and scope attributes survive MLflow ingestion. If necessary, copy service and FORGE identity into namespaced span attributes in the export payload so the native UI retains component identity; document the mapping and do not mutate the archived source record.

### Single and multiple runs

For a single test, link traces to the resolved run that receives that test's artifacts. For multiple tests, ingest each unique trace once and link it to its owning child run. The parent can retain the complete artifact tree and a child index without ingesting duplicate copies of all native traces.

Reuse `_mlflow_destination.yaml` for the job-level destination. Extend existing export state with a test-directory-to-child-run mapping; do not create a competing parent destination marker. Repeated export must resume child runs from that mapping rather than create new children every time. Never use display names alone as identity.

Scope receipts by tracking server, workspace, experiment, run, archive checksum, and exporter version. A successful receipt for one destination must not suppress export to another destination. Reject a conflicting destination for the same resume operation unless the caller explicitly selected a new export target.

### Retry and failure semantics

Track ingestion and run linking separately. If ingestion succeeds but linking fails, retry the missing linkage. Retry transient network errors, throttling, and eligible server errors with bounded backoff. Authentication, schema, and capability failures should terminate clearly.

An interrupted HTTP request may have been accepted remotely. Preserve IDs, reconcile remote state where supported, and test duplicate ingestion and duplicate linking on the selected server. Do not claim exactly-once delivery based only on a local checkpoint.

Attempt ordinary artifact upload even if native ingestion fails, and attempt trace handling independently of an unrelated report failure. Aggregate failures after diagnostics and receipts are written. Refresh the final receipt on the owning run and the parent artifact tree where applicable, because their initial file upload may precede native ingestion.

If the destination server is unavailable, retain local artifacts and recovery state and report export failure. Do not silently downgrade requested native ingestion to artifact-only success.

## Secret handling and trace sanitization

Treat traces as public artifacts from their first persisted form. Sanitization must happen before the Collector file exporter writes the spool, not merely before final MLflow upload.

Use a reviewed allowlist for resource, span, and event attributes. Preserve diagnostic numeric fields such as duration, token counts, queueing measurements, and routing scores where they contain no sensitive content. Disable or remove prompts, completions, request/response bodies, authentication headers, cookies, URL credentials/query strings, and arbitrary exception text. Span names, status descriptions, and event names also need review; an attribute-only filter does not cover them.

Do not enable a debug exporter that dumps payloads to logs. Apply the same policy to the archive, normalized report inputs, and native export. Verify compressed artifacts by decoding them in tests; the existing text censor is not a sufficient check for gzip/protobuf content.

Reuse vault lookup and MLflow secret parsing. Credential values stay inside the export process and must not pass through toolbox arguments, context, command text, or a serialized configuration object. Prefer an explicitly configured HTTP client to adding global environment credentials. If existing MLflow APIs require scoped environment changes, restore them in `finally` and ensure no DSL metadata capture runs inside that credential scope. Restore workspace settings as well.

The collector deployment itself needs no MLflow authentication. If any future resource contains a Kubernetes Secret, apply it through the non-persisting secret path with `handled_secretly=True`; never use `oc_apply()` for it. Error messages and receipts contain sanitized failure categories, never credentials or raw rejected trace payloads.

## Status and error handling

| Condition | Required outcome |
| --- | --- |
| Tracing disabled | No instrumentation, resources, or native trace request |
| Native export disabled | Preserve ordinary artifacts; report native export skipped |
| Invalid configuration or unsupported producer | Raise before benchmark execution |
| Collector readiness/storage failure | Fail setup and capture non-sensitive diagnostics |
| Benchmark failure | Preserve the primary failure; still attempt available trace capture and other finalizers |
| Collection/drain timeout | Retain valid partial data, mark incomplete, notify, and fail requested tracing |
| Zero spans with sampling at zero | Record an expected empty capture |
| No expected spans in a confirmed full-sampling smoke test | Fail tracing acceptance |
| Ambiguous ownership or conflicting duplicate spans | Preserve sanitized diagnostics and surface validation failure |
| Ingestion failure | Preserve archives and receipt; mark export failed and notify |
| Ingestion succeeds but linkage fails | Record the distinction and retry linkage to the same run |

Shared libraries raise typed errors; orchestration decides how to combine them with test results. Use `ci.add_notification_file()` for operational failures. Keep benchmark outcome, collection outcome, and export outcome distinct, while making the overall CI result nonzero when requested tracing fails.

## Caliper reporting

Provide a shared report helper that can run for any discovered trace bundle without requiring a new parser implementation in every project. Integrate it into common post-processing and replot paths, while retaining each project's existing benchmark plugin.

The first report should include observed services and operations, span counts and error status, duration distributions, latency percentiles, numeric attribute distributions with units and provenance, and capture-quality indicators. Distinguish span duration from producer-supplied attributes and keep sampling metadata visible.

Report multi-service traces separately from root/parent consistency. Do not label trace-derived latency as client end-to-end latency or treat sampled request traces as a replacement for Prometheus telemetry. Omit full payload rendering and keep request content out of HTML output.

Generate reports from retained artifacts so report fixes and replot operations need neither a running collector nor a repeat benchmark. Native ingestion must not depend on successful HTML generation.

## Delivery stages

### Stage 1 Collector and MLflow compatibility proof

Build an isolated synthetic trace fixture with multiple services, a root and children, an error, an event, a link, resource attributes, and fixed IDs/timestamps. Validate Collector → sanitized file → copied archive → MLflow ingestion → run linkage.

Deliver the pinned collector configuration, dependency decision, OTLP fixtures, and reproducible integration test. Establish payload-size behavior, service-attribute visibility, duplicate handling, flush/restart behavior, and OpenShift volume permissions. Use synthetic credentials/content sentinels to verify sanitization without real secrets.

Exit criterion: the artifact preserves the intended safe fields, the native trace has the correct hierarchy/timing and run association, and repeated export creates no duplicate logical traces or extra child runs. If the file exporter cannot meet the requirements, revise this design explicitly before adding project adapters.

### Stage 2 Shared collection and export

Implement the shared models, toolbox commands and templates, capture lifecycle, bundle validation, resumable destination mapping, native exporter, and status integration. Add configuration validation and standalone replay through the existing Caliper export entrypoint. Use synthetic bundles to cover both single-run and multi-run export before involving GPUs.

Exit criterion: a valid bundle can be captured, exported, retried, and recovered through shared code without importing either llm_d or rhaiis orchestration.

### Stage 3 llm_d integration

Integrate standard vLLM serving first, then EPP and P/D instrumentation for supported deployments. Exercise two model/profile/workload test nodes and failure-time capture. Update user documentation and add a small tracing smoke preset.

Exit criterion: standard and P/D smoke runs produce the expected component spans, valid artifacts, correct child ownership, and visible status. Unsupported configurations fail explicitly.

### Stage 4 rhaiis integration

Add the vLLM ServingRuntime adapter, per-workload timing/capture, and final reconciliation. Run at least two workloads on the same deployment with warmup enabled. Exercise a benchmark failure and confirm profiler behavior remains independent.

Exit criterion: each workload gets its own bundle and run association without contamination from neighboring workloads, warmup, or profiling.

### Stage 5 Shared reporting and operating guidance

Add Caliper reports/replot support, recovery instructions, volume cleanup instructions, and the small adapter contract for other projects. Measure tracing overhead using controlled tracing-off, standard, and detailed runs before recommending defaults for performance testing.

Exit criterion: both projects produce the same report contract, existing untraced tests remain unchanged, and operating guidance includes evidence from the compatibility and smoke tests.

## Validation matrix

| Area | Required tests |
| --- | --- |
| Configuration | Off/standard/detailed, ratio bounds including zero, YAML types, unsupported engines, flag conflicts |
| Rendering | Idempotent injection; standard/prefill/decode/EPP/proxy containers; existing non-tracing args/env preserved |
| Isolation | Two jobs in one namespace; cleanup restricted to ownership labels; reused deployment validation |
| Storage | OpenShift permissions, collector container and pod restart, no truncation, full volume, verified transfer |
| Collection | Late spans, partial final record, deadline expiry, duplicate spans, conflicting duplicates, missing root/parent |
| Attribution | Two sequential workloads; warmup exclusion; long requests crossing a boundary; overlapping-window rejection |
| Encoding | Hex IDs, nanosecond timestamps, typed attributes, events, links, resource and scope metadata |
| Sanitization | Sentinels in attributes, names, events, errors and URLs absent from spool, logs, archives, reports, and requests |
| MLflow | Actual endpoint/authentication, SQL backend, workspace and experiment routing, single and child runs |
| Retry | 429/5xx, timeout after acceptance, link failure after ingestion, repeated export, changed archive/destination |
| Failure lifecycle | Benchmark failure, capture failure, report failure, export failure, primary exception preservation |
| Disabled behavior | No resources, imports requiring optional tracing packages, injected configuration, or native requests |

Use unit tests for pure transformations and contract validation, DSL tests for lifecycle/error behavior, and a local SQL-backed MLflow integration test for the exporter. The pinned Collector container is needed to validate the actual file format and lifecycle. OpenShift smoke tests then establish controller-specific instrumentation and storage behavior.

Run affected existing suites in `projects/core/tests`, `projects/cluster/tests`, `projects/caliper/tests`, `projects/llm_d/tests`, and `projects/rhaiis/tests` as each integration lands. Run repository Ruff formatting and lint checks. Keep any live experiment/run creation limited to the explicitly selected validation environment and follow the repository's MLflow mutation authorization rules.

## Decisions required before release

| Decision | Evidence required |
| --- | --- |
| Collector image and file contract | Stage 1 replay, restart, flush, and storage tests |
| Client/server baseline | Actual native endpoint, auth proxy, workspace, linking, and duplicate-ingestion tests |
| Storage defaults and retention | Expected trace volumes and tested OpenShift storage behavior |
| Supported producer matrix | Image/controller-specific standard and P/D smoke results |
| Drain deadline and quality interpretation | Producer batching measurements and interrupted-run tests |
| Sanitization allowlist | Safe diagnostic-field review and sentinel tests for every persisted surface |
| Performance guidance | Controlled overhead measurements using the same workload and deployment settings |

Keep these decisions attached to test evidence. Do not describe an engine, topology, or recovery scenario as supported merely because the configuration can be rendered.

## Implementation amendment — 2026-10-28

The local pinned-Collector probe on 2026-10-27 accepted synthetic OTLP with
HTTP 200 and persisted prompt/completion sentinels before capture sanitization.
The pinned redaction processor only traverses resource/span attributes; it does
not implement the full reviewed event/link/nested-value policy. To preserve the
native file format and link identities with one policy implementation, the
collection path now inserts an in-memory Python sanitizer between the ingress
Collector pipeline and a loopback-only persistence pipeline. Neither raw queueing
nor raw persistence is permitted. Forwarding is synchronous and failures return
to the producer. The native file exporter remains the only spool writer.

The sidecar runs shared policy source mounted through the capture ConfigMap,
uses only Python's standard library, and has no writable spool mount. The
reproducible fixture is `projects/cluster/tests/test_tracing_collector_integration.py`;
operating instructions describe the opt-in local command and remaining live
release gates. This is an evidence-driven amendment to the original single
Collector processing step, not an alternative exporter or legacy fallback.
