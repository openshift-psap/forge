# Forge GitOps Configuration

This directory contains the ArgoCD GitOps configuration for deploying FORGE components including images and pipelines.

## Structure

```
gitops/
├── applications/           # ArgoCD Application manifests
│   ├── forge-development.yaml
│   ├── forge-production.yaml
│   └── kustomization.yaml
├── base/                  # Base Kubernetes manifests
│   ├── images/            # Image build configurations
│   │   ├── imagestream.yaml
│   │   ├── build.yaml
│   │   ├── buildrun.yaml
│   │   └── kustomization.yaml
│   ├── workflows/         # Tekton pipelines and tasks
│   │   ├── task-forge-step.yaml
│   │   ├── pipeline-full.yaml
│   │   ├── pipeline-test-only.yaml
│   │   ├── pipeline-replot.yaml
│   │   └── kustomization.yaml
│   └── kustomization.yaml
└── overlays/             # Environment-specific configurations
    ├── development/
    │   └── kustomization.yaml
    ├── production/
    │   └── kustomization.yaml
    └── intlab-production/
        └── kustomization.yaml
```

## Components

### Images
- **ImageStream**: Manages forge-core image tags and references
- **Build**: Shipwright build configuration for building forge image from source
- **BuildRun**: Triggers the build process

### Workflows
- **Task**: `forge-step` — reusable Tekton task for executing forge commands
- **Pipelines**:

| Pipeline | Steps | Finally | Use case |
|---|---|---|---|
| `forge-full` | pre-cleanup → prepare → preflight → test | post-cleanup → export-artifacts | Full lifecycle with cleanup on both ends |
| `forge-prepare-test` | prepare → preflight → test | export-artifacts | Prepare + test without cleanup |
| `forge-test-only` | preflight → test | export-artifacts | Skip preparation, just run the test |
| `forge-prepare-only` | prepare → preflight | export-artifacts | Set up the cluster without benchmarks |
| `forge-replot` | replot | export-artifacts | Re-run visualization/postprocessing |

Select a pipeline via `spec.pipeline` in the FournosJob CR (default: `fournos-full`).

### FournosJob `spec.env` variables

The `forge-step` task bootstrap script reads certain well-known variables
from `spec.env` and acts on them before launching the execution engine.

#### Git checkout

| Variable | Description |
|---|---|
| `PULL_PULL_SHA` | Checkout a specific git commit SHA. The task runs `git fetch origin <sha>` and `git reset --hard FETCH_HEAD` before executing. |
| `PULL_NUMBER` | Checkout the HEAD of a GitHub PR (`refs/pull/<number>/head`). Also enables GitHub PR notifications — test results are posted as a comment on the PR. |
| `PULL_BASE_SHA` | Base branch commit SHA (informational context for the execution engine). |

If neither `PULL_PULL_SHA` nor `PULL_NUMBER` is set, the execution engine
runs with the code baked into the container image.

`PULL_NUMBER` is the most convenient option for testing a PR: it checks
out the latest code from that PR **and** posts results back to GitHub.

#### Example

```yaml
spec:
  env:
    PULL_NUMBER: "118"        # checkout PR #118 HEAD + post results to PR
```

```yaml
spec:
  env:
    PULL_PULL_SHA: "abc123"   # checkout exact commit, no PR notification
```

## Environments

### Development (`fournos-dev` namespace)
- Auto-sync enabled with prune and self-heal
- Deploys from main branch
- More aggressive sync policies for faster iteration

### Production (`fournos-prod` namespace)  
- Conservative sync policies (no auto-prune)
- Manual approval recommended for critical changes
- Extended revision history

### Intlab production (`psap-automation` namespace)

The Intlab cluster does not run the OpenShift internal image registry. Use
`overlays/intlab-production`, which keeps the shared Forge manifests but points
all Fournos resolver Jobs at `quay.io/rh_perfscale/forge:latest`. The
management cluster must continue using `overlays/production`, whose resolver
annotation points at its internal `forge-core` ImageStream.

Apply the Intlab-specific Application definition only in the Intlab cluster:

```bash
oc apply -f gitops/applications/forge-intlab-production.yaml
```

## Deployment

### Option 1: Deploy Applications Directly

This applies the default application set, including the management
`forge-production` definition. Do not use it for Intlab; use the
Intlab-specific Application definition below instead.

```bash
oc apply -k gitops/applications/
```

### Option 2: Deploy to Specific Environment
```bash
# Development
oc apply -k gitops/overlays/development/

# Production  
oc apply -k gitops/overlays/production/

# Intlab production
oc apply -k gitops/overlays/intlab-production/
```

### Option 3: Manual Application Creation
```bash
oc apply -f gitops/applications/forge-development.yaml
oc apply -f gitops/applications/forge-production.yaml
```

## Integration with Fournos

This GitOps configuration replaces the forge-specific manifests previously stored in the `fournos/config/forge/` directory. The manifests have been adapted to:

1. Use dynamic namespace placeholders that get resolved by kustomize overlays
2. Follow GitOps best practices with environment-specific configurations
3. Include proper labeling and annotations for resource management
4. Support both automated and manual deployment workflows

## Migration Notes

The following changes were made from the original fournos configuration:
- Moved from `fournos/config/forge/` to `forge/gitops/`
- Added kustomization files for proper resource management
- Created environment-specific overlays for dev/prod deployments
- Updated image references to use dynamic namespace resolution
- Added ArgoCD Application manifests for automated GitOps deployment
