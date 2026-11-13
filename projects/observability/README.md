# Forge observability

Forge can create and remove a Thanos Receiver for each observability-enabled
Forge run. The OpenShift cluster and user-workload Prometheus instances send
selected metrics to that Receiver. It stores samples on a run-owned PVC and
uploads TSDB blocks to S3. The central Thanos Store Gateway reads the bucket,
and the central Querier reads from the gateway. The Querier does not accept
remote writes.

The lifecycle is implemented in Forge. It does not deploy the target cluster
ingestion plane from the separate `observability` repository or depend on its
already-running Receiver. Forge still uses the target cluster's Prometheus
instances and the existing central Store Gateway and Querier, which must read
the same S3 bucket. The Receiver runs inside the target cluster so Prometheus
can reach its remote-write endpoint.

Hera's existing unnamed remote-write queues still point to the Receiver in
`kserve-e2e-perf`. Forge's named queues write to a separate run Receiver. Retire
the old queues and Receiver as a planned migration before relying on Forge as
the sole target-cluster ingestion path; if both filters select the same series,
both Receivers can upload overlapping blocks to S3. Forge does not change or
delete those old resources.

## Run lifecycle

1. The host project validates the run and claims its workload namespace. The
   lock prevents two Forge runs from using the same namespace at once. Forge
   creates a run-specific Receiver namespace, Secret, Service, StatefulSet and
   PVC, then adds named queues to the cluster and user-workload monitoring
   ConfigMaps. It preserves unrelated ConfigMap entries.
2. The host project creates its ServiceMonitors, deploys the workload and runs
   its benchmark. The Receiver stores samples locally and periodically uploads
   blocks to S3.
3. After the workload stops, Forge drains the named queues on every Prometheus
   replica, removes the queues, and waits for them to disappear. It then asks
   Thanos to shut down gracefully and verifies the final TSDB flush and S3
   upload before deleting the Receiver namespace and PVC. If Forge cannot prove
   the upload, teardown fails and retains the PVC and workload namespace lock
   for recovery.
4. Forge releases the workload namespace lock only after Receiver teardown
   succeeds. Uploaded blocks remain in S3 for central queries.

Thanos normally uploads blocks about every two hours, so a short benchmark
needs the final flush before its PVC is deleted. The central Store Gateway may
take its configured sync interval to discover newly uploaded blocks. The
checked-in central deployment uses a five-minute sync interval. See the
[Thanos Receive documentation](https://thanos.io/v0.35/components/receive.md/)
and the [v0.35 shutdown implementation](https://github.com/thanos-io/thanos/blob/v0.35.0/cmd/thanos/receive.go).

## Configuration

The standalone project reads
[`orchestration/config.yaml`](orchestration/config.yaml). `rhaiis` and `llm_d`
also have an opt-in `observability` section and call the same lifecycle from
their CI phases. `observability.enabled` defaults to `false`. Fournos uses its
stable `FJOB_NAME` as the run ID; local runs must set `observability.run_id`.
An explicit run ID in Fournos must equal `FJOB_NAME`. Per-run setup and teardown
require the `forge-full` pipeline, which runs `post-cleanup` even when the
benchmark fails.

The workload namespace stays at the host project's configured namespace. The
run's ID is stored in a Namespace annotation while the lock is held. Forge
selects only this namespace in remote-write filters. Workload namespaces must
be exclusive to one Forge run during the lifecycle; unrelated active workloads
in that namespace can add samples and cause teardown to wait rather than delete
data. RHAIIS's existing namespace also needs its `model-pvc`, `storage-config`
Secret and any configured image pull Secrets; Forge leaves these workload
assets in place.

`receiver.namespace` and `monitoring.queue_name` are base names. Forge appends
a deterministic run suffix, so Receiver resources and queues are unique across
runs. Set `cluster_label` to the target cluster name. The Receiver adds
`forge_run_id` as an external block label so uploaded blocks can be queried by
run after the Store Gateway syncs.

Put a complete Thanos `type: S3` object-store YAML in the `psap-forge-thanos`
vault as `objstore.yaml`, or set another `receiver.objstore_vault`. It must
target the bucket and prefix read by the central Store Gateway. Do not put S3
credentials in Forge project configuration because configuration can appear
in public artifacts. Forge applies the Secret through `oc` standard input and
does not write it to an artifact.

RHAIIS creates a ServiceMonitor for its predictor Service. llm_d creates its
workload and scheduler ServiceMonitors when
`deployments.defaults.enable_monitors` is enabled. The UWM queue forwards
metrics from the workload namespace; the cluster queue forwards selected
container CPU, memory and pod-label series. GPU forwarding is off by default.
It can be enabled with `monitoring.forward_gpu_metrics: true`; the filter uses
`exported_namespace` to keep only GPU series attributed to the workload
namespace. DCGM series without that label are excluded because they cannot be
assigned safely to one run.

To recover a failed teardown, stop or clean the workload and rerun that host
project's `post-cleanup` with the same `FJOB_NAME` (or the same local
`observability.run_id`) and observability settings. The retained namespace lock
prevents another Forge run from taking over before the upload is verified.

The account running Forge needs permission to create, read and delete the
Receiver namespace and its Secret, StatefulSet, Service, PVC and Pod; to read
service accounts and generated service-account Secrets in that namespace; to
create and delete configured ServiceMonitors; to update the cluster and
user-workload monitoring ConfigMaps; to read Prometheus resources and metrics
from all replicas; and to read Receiver logs and exec into its Pod for final
shutdown. The Receiver needs a usable StorageClass. Leave
`receiver.run_as_user` and `receiver.fs_group` unset unless the cluster's
Security Context Constraints require explicit values.

OpenShift's user-workload monitoring capability remains enabled after a run.
Forge removes only its named queues and leaves unrelated monitoring settings
untouched. This project does not deploy or change the central query plane.
