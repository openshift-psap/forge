# Inference Playbooks validation

Select a recipe by its consecutive directory path beneath `models/`,
`<model>/recipes/<recipe-folder>`, and provide an explicit manifest path relative
to that recipe folder:

```yaml
inference_playbooks:
  recipe: kimi-k3/recipes/h200-x8-pp2-tp8-8k1k
  manifest_path: manifests/vllm-v0.29.0/leaderworkerset.yaml
  namespace: provisioned-test-namespace
```

The recipe resolves to
`models/kimi-k3/recipes/h200-x8-pp2-tp8-8k1k/recipe.yaml`. Its declared `recipe_id`
is used for labels and evidence; the supplied locator is not rewritten in config.
Opaque recipe IDs, shortened `<model>/<recipe-folder>` selectors,
repository-root paths, absolute paths, traversal and symlink aliases are not
accepted. There is no repository scan or implicit path fallback for a selected
launch.

The explicit path selects one declared platform. Forge checks **all** immediate
YAML/YML files in that platform's manifest directory, including all documents.
By default, only an absent object namespace may be filled from the selected
target namespace; explicit namespaces must match. Services, Secrets,
unsupported objects and hidden serving repairs are rejected. Namespace
provisioning, storage, credentials and network prerequisites remain external.

With no recipe selected, the existing catalog-only validation performs no launch.
Bundle artifact filenames and repository-relative provenance paths are unchanged.

For a Deployment recipe on a mixed-GPU cluster, CI can select a verified node
without editing the recipe checkout:

```text
/var inference_playbooks.deploy.node_selector: {kubernetes.io/hostname: <verified-hostname-label-value>}
```

Use a label value confirmed on the target node. The override is empty by default.
Forge preserves `source-bundle.yaml`, records
the selector in `bundle-metadata.yaml`, and submits `submitted-bundle.yaml`.
It rejects conflicts with an authored selector or `nodeName`. A run with this
override tests the recipe under a CI scheduling constraint, not the unmodified
customer manifest; verify the pod's actual placement separately.

## AIPerf concurrency sweeps

Each concurrency workload invocation receives a unique `sweep_id`, persisted in
each member's Caliper metadata. Repeating the same workload starts a new sweep.
Individual runs retain their scalar KPIs. When rendering `kpis/kpis.html`, Caliper
uses a transient `KpiSweep` reporting group to draw concurrency-sorted series.
These series never merge or average the underlying runs, add artifact directories,
or create extra entries in `kpis.json` or MLflow metrics.

Members must have matching workload, model, deployment and other non-axis labels.
Conflicting labels or duplicate concurrency points are errors. Missing metrics
are omitted, not replaced with zero. Artifacts without `sweep_id` remain scalar
results; sweep membership is never inferred from directory names or metric IDs.

## LWS model storage

LeaderWorkerSet manifests may use existing PVCs or node-local `hostPath` volumes,
including both in the same workload. Forge preserves the authored volumes,
mounts and scheduling constraints; it does not create PVCs, populate host paths,
or rewrite storage. For a prepopulated host directory, use the following pod-spec
fragment in both `spec.leaderWorkerTemplate.leaderTemplate.spec` and
`spec.leaderWorkerTemplate.workerTemplate.spec`, alongside the other container
settings:

```yaml
containers:
  - name: vllm
    volumeMounts:
      - name: model
        mountPath: /models
        readOnly: true
volumes:
  - name: model
    hostPath:
      path: /var/lib/models/example
      type: Directory
```

Configure the server to load the model from the container's mount path. The host
directory and identical model weights must already exist on every eligible node;
use authored node selectors or affinity to constrain scheduling as needed. The
workload's service account must have an SCC permitting hostPath, and node
permissions and SELinux labels must allow the container to read the files. Forge
does not grant these privileges or change node permissions. No additional Forge
storage setting or `deployment.model_cache` hint is required for a v4 launch.

**LLMInferenceService does not support hostPath.** Catalog validation and selected
launches reject hostPath volumes in any LLMInferenceService pod template,
including worker and prefill templates, before writing launch artifacts or
contacting the cluster. Use PVC-backed storage for that deployment kind.
