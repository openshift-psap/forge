# Raw MLflow write stress workloads

These k6 scripts deliberately bypass the MLflow SDK. They stress the two
server-side write paths directly:

- `trace_meta_write.js`: `POST /mlflow/api/3.0/mlflow/traces` with the MLflow StartTraceV3
  JSON request.
- `span_cold_start.js`: sends one-span OTLP/HTTP protobuf requests for a new
  trace. The first span exercises MLflow's missing-`trace_info` creation path.
- `span_warm_start.js`: creates `trace_info` first, then sends one-span OTLP
  protobuf requests for that existing trace.

The span payload is generated deterministically in the k6 VU from
`SPAN_SIZE_BYTES`; static payload files are intentionally not used. Static
payloads would reuse trace/span IDs under a long run and would measure upserts
and conflicts rather than new production-like writes. It also avoids checking
large 1 MiB/10 MiB binary fixtures into git.

Useful environment variables:

```text
MLFLOW_TRACKING_URI=https://.../mlflow
MLFLOW_TOKEN=<optional bearer token>
USERS=100
TENANT_COUNT=1
USERS_PER_TENANT=100
DURATION=60s
WARMUP_RAMP_UP=false
SPAN_SIZE_BYTES=1048576
SPANS_PER_TRACE=5
TRACES_PER_EXPERIMENT=4
```

The scripts expose separate `trace_metadata_latency` and `span_latency` k6
trends, plus request counters and `write_failures`. The span endpoint receives
exactly one span per HTTP request, so `span_latency` is the actual latency of
one span-write request; it is not a batch-normalized metric.

For a multi-tenant cell, set `TENANT_COUNT` and `USERS_PER_TENANT`; `USERS`
must be their product. Each VU stays on one workspace for its run. Workspaces
are named `tenant-1` through `tenant-N`, and experiment pools are created
separately in each workspace. Forge's matrix accepts cells such as:

```yaml
experiment.write_k6.matrix_cells:
  - {span_size_bytes: 102400, tenant_count: 1, users_per_tenant: 10}
  - {span_size_bytes: 102400, tenant_count: 10, users_per_tenant: 10}
```

`experiment.concurrency` supplies `users_per_tenant` for generated matrix
cells, while `experiment.write_k6.tenant_count` supplies the tenant counts.

Before the VUs start, k6 `setup()` creates
`ceil(USERS_PER_TENANT / TRACES_PER_EXPERIMENT)` uniquely named experiments
in each tenant workspace and passes their IDs to the VUs. Experiment-creation requests are tagged
`phase=experiment_setup` and are not added to the workload latency counters.
With 128 users per tenant and `TRACES_PER_EXPERIMENT=4`, 32 experiments are
created per tenant and four VUs use each experiment. During a duration-based
run, each VU continues to create additional traces in its assigned experiment.

Forge can set `experiment.write_k6.ramp_up_warmup: true` to run the separate
warmup Job with k6's `ramping-vus` executor, increasing from 0 to `USERS` over
the warmup duration. The measured Job remains a constant-VU run.

If no URI is supplied, the scripts default to:

```text
MLFLOW_TRACKING_URI=https://mlflow.redhat-ods-applications.svc.cluster.local:8443/mlflow
```

The benchmark checkout currently has only one MLflow tag (`nightly`), and that
tag does not contain a benchmark payload fixture. The protobuf field layout is
therefore implemented from the checked-out MLflow OTLP and service schemas,
while payload sizes and IDs are generated at runtime.
