# IBM Cloud create/destroy pipeline

`forge-ibmcloud-create-destroy` runs two Forge tasks: **create**, then **destroy**
in Tekton's `finally` section. No workload test phase is included yet.

The wrapper calls these OpenShift release steps:

1. `ipi-conf`
2. `ipi-conf-ibmcloud`
3. `ipi-conf-ibmcloud-manual-creds`
4. `ipi-install-install`
5. `ipi-deprovision-deprovision` (in the destroy task)

`toolbox/create/main.py` provides the create DSL entrypoint and named tasks for validating
inputs, preparing the private workspace and scripts, configuring the cluster
and operator credentials, and running installation. `toolbox/destroy/main.py` provides a
separate DSL entrypoint for checking installation state, loading saved inputs,
running teardown, and removing private outputs. Both use `@entrypoint`, `@task`,
and `execute_tasks()` for normal Forge task logging and failure reporting.
`toolbox/public.py` defines the public contract: `ClusterConfig` is a strict
Pydantic model for the standalone settings YAML (the content of `ibmcloud`,
without that outer key). Orchestration validates the configuration, writes
`ibmcloud-settings.yaml` in the private shared workspace, and passes its path as
`settings_file_path`. Create validates the file before preparing installation
state. Unknown fields, wrong types, invalid counts/zones, and unpinned releases
are rejected. Defaults remain in the orchestration configuration.

The same public module exposes `validate_vault_directory(vault_dir, for_create=...)`
and the required filename lists. Orchestration and toolbox entrypoints validate
that required files are readable and nonempty. Create requires the full bundle;
destroy requires only `ibmcloud-api-key`. Validation never logs credential contents.
Standalone callers can use these contracts before invoking either toolbox.

The configuration step uses a private, compact JSON copy of the pull secret so
pretty-printed vault JSON remains valid when interpolated by the release script.
The vault mount is never modified. Resource-group names are restored as strings
before saving YAML, preserving names such as `on` that YAML otherwise treats as Booleans.

`toolbox/common.py` downloads the scripts and their default environment definitions
from a pinned release commit. It adapts hardcoded installer paths to persistent
storage, fixes upstream's digest-pullspec normalization, and supplies the small
lease shim needed outside OpenShift CI. Installation and cloud deletion stay
in the release scripts. There is no separate CLI, state manager, runner image,
or container engine.

The shared `run_step_command()` helper redirects release-script stdout and stderr directly
to internal files and reports a safe error on failure. Generated credential
contents are never put in DSL arguments, task results, or context: Forge saves
those as public execution metadata. Task context contains only paths, non-secret
inputs, and safe status information.

## Configuration and credentials

Use the normal Forge project `ibmcloud_cluster` and its `configOverrides`. Example job:

```yaml
apiVersion: fournos.dev/v1
kind: FournosJob
metadata:
  generateName: ibmcloud-create-destroy-
spec:
  pipeline: forge-ibmcloud-create-destroy
  clusterless: true
  exclusive: false
  executionEngine:
    forge:
      project: ibmcloud_cluster
      configOverrides:
        ibmcloud.base_domain: clusters.example.com
        ibmcloud.resource_group: forge-tests
        # Replace with an existing amd64 OpenShift payload digest.
        ibmcloud.release.image: quay.io/openshift-release-dev/ocp-release@sha256:REPLACE_ME
  # The standard Forge resolver supplies secretRefs: [ibmcloud-ipi].
```

Fill in:

- `ibmcloud.base_domain`: an authoritative domain configured in IBM CIS.
- `ibmcloud.resource_group`: the existing IBM resource group.
- `ibmcloud.release.image`: an amd64 OpenShift payload pinned by SHA256 digest.

Defaults live in `projects/ibmcloud_cluster/orchestration/config.yaml`: Sydney `au-syd`,
zone `au-syd-1`, three `bxf-8x32` control-plane nodes and three `bxf-16x64` workers. Those
values are configurable through normal overrides: `ibmcloud.control_plane.count`,
`ibmcloud.control_plane.type`, `ibmcloud.workers.count`, and `ibmcloud.workers.type`.
`ibmcloud.release.commit` pins the openshift/release scripts revision;
`ibmcloud.release.image` selects the OpenShift payload.
Tekton sets the task timeouts: four hours for create, 59 minutes for destroy.

The standard Forge resolver requests the `ibmcloud-ipi` vault bundle, declared
in `vaults/ibmcloud-ipi.yaml`. Fournos mounts it using its existing read-only
vault volume. It is a bundle name, not a new secret service. Required files:

| Filename | Purpose |
| --- | --- |
| `ibmcloud-api-key` | Infrastructure create/delete credentials |
| `ibmcloud-cloud-controller-manager-api-key` | Cloud controller credentials |
| `ibmcloud-cluster-csi-drivers-api-key` | CSI driver credentials |
| `ibmcloud-image-registry-api-key` | Image registry credentials |
| `ibmcloud-ingress-operator-api-key` | Ingress operator credentials |
| `ibmcloud-machine-api-api-key` | Machine API credentials |
| `pull-secret` | Registry authentication JSON |
| `ssh-publickey` | OpenSSH public key |
| `ssh-privatekey` | Matching private key without a passphrase |

The IBM account needs IAM permissions and quota for the cluster, temporary
bootstrap machines, and deletion. The pipeline does not create the resource
group, CIS domain, or IAM identities. Do not put credentials in overrides or
`spec.env`: Forge logs those inputs.

## Workspace contract

The pipeline declares only the existing `artifacts` workspace. Fournos supplies
its normal `volumeClaimTemplate`, and Tekton creates the per-run PVC. Each
create/destroy task maps two subdirectories of that same pipeline workspace:

| Task workspace | PVC subdirectory | Contents |
| --- | --- | --- |
| `artifacts` | `public/` | Forge task logs and safe status summaries |
| `internal` | `internal/` | Saved release scripts, input record, installer assets, kubeconfig, credentials and raw logs |

Both tasks mount the same directories at the same paths. The wrapper uses an
owner-only `internal/ibmcloud` directory. Raw upstream output is redirected
there; it is never streamed into the public task logs. Only the artifact
workspace is eligible for export. This two-task pipeline does not include an
artifact-upload task. Any future export task must also bind only the `public/`
subpath, never the PVC root. No Fournos operator or RBAC changes are required;
other pipelines retain their existing workspace mappings.

The task provides the internal workspace path through `FORGE_SHARED_DIR`.
Forge's `env.init()` resolves it to `env.FORGE_SHARED_DIR` as a `Path` and creates
the shared root if needed. This workspace is optional for other jobs and for
the resolver; only the IBM lifecycle requires it. Artifact-directory changes
do not change the shared path or publish its files.

The internal workspace is intentionally writable: the installer must save its
assets there. Its file contents must not be copied into public artifacts or
uploaded. Internal path strings can appear in Forge's environment snapshots,
DSL arguments, and context; those contain paths, not the credential file contents.
Forge does not have a global blacklist for a directory named `internal`. The
current pipeline protects it through the task subpath mappings, private output
redirection, and keeping credentials out of DSL context. A task mounting both
paths can still explicitly copy between them; future artifact-export tasks
should mount only `public/` and must not introduce links to private files.

Orchestration passes `FJOB_NAME` explicitly as the create toolbox's `job_name`.
Cluster names contain the job name (shortened when necessary, retaining its
beginning and ending) plus a random suffix, with a maximum of 27 characters.
For example, `ibmcloud-create-destroy-abcde` produces a name like
`ibmcloud-abcde-0123456789ab`. The full job name and generated cluster name are
saved together in private inputs and in the create phase's public status and DSL
metadata. Destroy verifies the saved cluster name rather than generating one.
Standalone callers must supply their own `job_name` run identifier. Cloud resource
tags for owner, project, and job are left for a separate change.

The small `inputs.json` file saves non-secret upstream inputs so destroy uses
the same cluster name, scripts, and payload. Installer assets are written on
the PVC throughout creation, including before failures. Tekton controls task
ordering and timeouts; the OpenShift installer maintains cloud resource state.

Destroy runs after create success or failure. If installation never started,
it does nothing. If installation started but metadata is missing, it fails
visibly instead of reporting successful cleanup. Failed teardown retains all
internal files for recovery; successful teardown removes generated credentials
and runtime files, retaining the scripts, inputs, and completion markers.
After cloud deletion is recorded, retries finish any interrupted local cleanup
without repeating cloud operations or requiring installer metadata or credentials.
The create phase saves only `ibmcloud.resourceGroupName` from installer
`metadata.json` as `ibmcloud-resource-group.json` in public artifacts, including
after installation failure when metadata exists. Destroy saves the same field
before deleting private installer assets. This artifact contains only
`{"resourceGroupName": "..."}`; the value is not printed by the artifact writer,
and the full metadata file remains private. This identifier helps locate resources;
it does not replace the metadata and credentials needed for installer teardown.
An artifact-writing failure is reported but does not skip cloud teardown.

Teardown failures create a safe Forge notification file instructing retention
of the job, PipelineRun, and private PVC state; notification delivery requires
the normal Forge notification/export handling.

Use graceful Fournos `Stop` to run finally. `Terminate`, deleting the job, or
loss of the hub cluster can bypass teardown. Keep a failed job, its PipelineRun,
and its PVC until recovery is complete. Deleting those resources or running
Tekton pruning can remove the saved installer state.
There is no independent cloud-resource expiry controller.

## Rollout

Rebuild the normal Forge image and deploy the updated `forge-step` Task and
new Pipeline from `fournos/gitops/base/workflows`. The full and lightweight
Forge images include the required SSH/GNU tools; execution targets Linux amd64.

Supply the vault bundle and three required configuration values before
submitting the example job. Nothing is deployed by editing these files.

## Local validation without cloud keys

From the Forge checkout, run in an environment with Forge's Python dependencies:

```bash
python3 -m unittest projects.ibmcloud_cluster.tests.test_ibmcloud projects.ibmcloud_cluster.tests.test_ibmcloud_release -v
```

These tests exercise the actual DSL and configuration override handling with
dummy credential files, simulated installation, failure recovery, and checks
that private file contents do not appear in public artifacts. Additional tests
run the real artifact collector with its upload backend mocked while private
files still exist, after installation and teardown failures, and through the
Fournos default export-root selection. With an adjacent
`release` checkout, they also check adaptations against the pinned committed
scripts and run the real configuration and teardown scripts. Teardown uses fake
`oc` and installer executables; no cloud API is called. On macOS, that teardown
test requires GNU `gcp`; it skips if unavailable. Source-dependent tests skip
when the adjacent checkout is absent.

The Task, Pipeline, and an inline TaskRun binding both subpaths to one PVC were
also accepted by the configured cluster in server-side dry-run on 2026-11-24.
That check created no resources. Tekton's controller implementation was reviewed
to confirm both mappings derive the same claim name and retain their subpaths.

The original system-package layers from both Forge Containerfiles were built
locally with Docker for Linux amd64 on 2026-11-24, without adding packages.
Both already contained the required commands, including SSH tools, GNU `find`,
`awk`, `grep`, `sed`, `tar`, `gzip`, and `timeout`. Checks passed for GNU `cp -ar`
and `cp -t`, `sed -i`, `grep -P`, `timeout --foreground`, and Bash `[[ -v ]]`.
All five pinned adapted release scripts passed `bash -n` in both containers.
No Containerfile changes are needed for these dependencies. This check covers
the original base and RPM installation layers, not the full Python application
image build. The probe containers ran with networking disabled.

These checks do not execute the full upstream installation script or validate
live PVC mounting, IBM IAM, quota, DNS, or real installation and deletion. A
Linux/OpenShift-hosted live create/destroy trial remains required.
