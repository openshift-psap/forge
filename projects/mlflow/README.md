# MLflow performance project

## Standalone toolbox commands

Run these toolbox commands from the Forge root to see the inputs that CI uses.

```bash
python -m projects.mlflow.toolbox.run_k6_read.main --help
python -m projects.mlflow.toolbox.run_k6_write.main --help
```

Both commands use an existing MLflow endpoint and save the k6 logs, Kubernetes
manifests, and Forge restart command in their artifact directory. For read
benchmarks, run `seed_postgres` and `export_trace_data`, then pass the exported
`trace_data.json` with `--trace-data-path`. The workloads use the pod's service
account to authenticate to MLflow.

Use `manage_placement` to select and label nodes and storage. It writes
`placement.json` with the selectors and StorageClasses for deployment and k6.
After resource cleanup, pass that file to `manage_placement --release-from`
to remove the temporary labels.

Use `manage_tenant_workspaces --tenant-count N --k6-namespace NAMESPACE`
to set up workspace namespaces and grant the k6 service account access.
After the run, pass the generated `workspaces.json` to
`manage_tenant_workspaces --cleanup-from` to undo the changes.

## RHOAI version selection

The `prepare` phase uses Forge's shared RHOAI operator and DataScienceCluster
toolboxes. Its defaults are in
[`orchestration/config.d/platform.yaml`](orchestration/config.d/platform.yaml).

Set `platform.operators.rhods-operator.channel` to the desired OLM release
stream. For an exact EA/RC release, set the channel and enable a custom catalog
with the immutable catalog image digest for that release. This is the same
channel/catalog selection used by Forge's RHOAI setup. The CI prepare phase
loads the RHOAI pull-secret vaults when custom catalog mode is enabled.

An existing DataScienceCluster is reused without replacing its other component
settings; a clean cluster gets the project's minimal MLflow-enabled
DataScienceCluster.

Run prepare from the Forge root with:

```bash
python3 -m projects.mlflow.orchestration.cli prepare
```

## PostgreSQL test version

Test cells use `registry.redhat.io/rhel9/postgresql-18:latest`. The
infrastructure deploy step checks `SHOW server_version_num` after PostgreSQL
starts and aborts the cell unless the server reports major version 18.

Cases with a positive `experiment.pg_size_gb` require a local seed fixture.
Place it at `projects/mlflow/toolbox/seed_postgres/data/seed_trace.json` before
running a seeded preset or the standalone `seed_postgres` command. Git ignores
this path. Write cases with `experiment.pg_size_gb: [0]` can run without it.

The infrastructure toolbox generates credentials for PostgreSQL, its metrics
exporter, and MinIO for each test case. A deployment retry reuses the Secrets
already in the test namespace. Cleanup removes the credentials after the
workloads and their PVCs have been removed.

## Storage and node placement

Before each test cell, the orchestration discovers storage and node placement
from the cluster. It prefers an LVMS/TopoLVM StorageClass backed only by NVMe
devices, verified from resolved per-node device paths when available or from
the LVMVolumeGroup selector and TopoLVM node capacity annotations.
If LVMS is unavailable, it can use available Local Storage Operator PVs whose
paths identify NVMe. It fails before deployment if it cannot verify a usable
NVMe-backed storage option with enough capacity for PostgreSQL and MinIO.

The test selects two distinct Ready worker nodes: PostgreSQL and MinIO share
the NVMe storage node, while MLflow and k6 share the compute node. Temporary
labels drive pod scheduling; local PV claims also get selectors for the exact
NVMe PVs selected. The labels are removed during cell cleanup. The selected
StorageClass and nodes are recorded in test metadata. No node hostname or
StorageClass name needs to be configured for a particular cluster.

## Concurrent read/write workload

Use the `mixed-read-write-get-trace` preset to run separate read and write k6
Jobs concurrently. Both use `experiment.duration_seconds` and
`experiment.warmup_duration_seconds`, so their configured warmup and measured
durations match. Both k6 Jobs finish setup before the runner releases measured
traffic. The runner resets PostgreSQL statistics after warmup and setup, stores
the read and write k6 summaries separately, and uses the overlap of the two
k6-reported measurement windows for Prometheus capture.

The preset defaults to 8 read VUs, 4 write VUs, 1 tenant, a 10 GiB PostgreSQL
database, 30 seconds of warmup, and 180 seconds of measurement. Override values
with `000__ci_metadata/variable_overrides.yaml`, for example:

```yaml
experiment.workload: mixed
experiment.mixed_workload.read_concurrency: 16
experiment.mixed_workload.write_concurrency: 4
experiment.mixed_workload.tenant_count: 1
experiment.read.operation: get_trace
experiment.pg_size_gb: 500
experiment.seed.traces_per_experiment: 10
experiment.warmup_duration_seconds: 30
experiment.duration_seconds: 180
```

Mixed concurrency values are total VUs per workload across all tenants and
must divide evenly by the tenant count. Read options remain under
`experiment.read`; write mode, payload size, spans per trace, and experiment
pool settings remain under `experiment.write_k6`.

For write tests, set `experiment.write_k6.ramp_up_warmup: true` to ramp k6
from zero to the configured VU count across `experiment.warmup_duration_seconds`.
This applies only to the separate, unmeasured warmup job. The measured write
job still starts at its configured constant VU count.

MLflow server replicas and Gunicorn workers can also be matrix dimensions.
Set `experiment.mlflow.replicas` and `experiment.mlflow.workers` to scalars or
lists; each replica/worker combination becomes a separate cell and is applied
to the MLflow custom resource before the workload starts. The default server
image is `quay.io/opendatahub/mlflow:odh-stable` with `Always` pull policy.
Override it with `experiment.mlflow.image` and
`experiment.mlflow.image_pull_policy`; use a digest reference for repeatable
benchmarks. Defaults are one replica and four workers. For example:

```yaml
experiment.mlflow.replicas: [1, 2, 4]
experiment.mlflow.workers: [4, 8]
experiment.mlflow.image: quay.io/opendatahub/mlflow:odh-stable
experiment.mlflow.image_pull_policy: Always
```

Each matrix cell recreates and seeds its own PostgreSQL database, so combining
these dimensions with large database sizes multiplies setup time substantially.

Run the preset from the Forge root with:

```bash
python projects/mlflow/orchestration/cli.py --preset mixed-read-write-get-trace test
```
