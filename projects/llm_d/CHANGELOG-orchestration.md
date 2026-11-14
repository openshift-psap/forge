# LLM-D Orchestration Changelog

## 2026-07-21 - Enhanced P/D Configuration System

### P/D Profile Name-Based Configuration
- **FROM_NAME Parameter Extraction**: Implemented automatic parameter extraction from P/D profile names
  - **Pattern**: `pd-d.x{decode_pods}-p.tp{prefill_tp}-d.tp{decode_tp}-p.x{prefill_pods}`
  - **Tensor Parallelism**: Separate extraction for prefill (`p.tp4`) vs decode (`d.tp4` or `tp4`) containers
  - **Pod Replicas**: Separate extraction for prefill (`p.x8`) vs decode (`d.x2` or `x2`) pods
  - **Strict Parsing**: Removed fallback logic for precise parameter extraction

### Template-Based Deployment Profiles  
- **Template System**: Added `template__pd` with `FROM_NAME` placeholders for dynamic configuration
  - **Auto-Resolution**: Profile names automatically populate `tensor_parallelism`, `prefill_pods`, `decode_pods`
  - **Scheduler Integration**: Template profiles support simplified scheduler configuration (`scheduler_manifest: {}`)

### Global P/D Configuration Architecture
- **Centralized P/D Config**: Moved P/D-specific settings to global `deployments.pd` configuration
  - **VLLM Extra Args**: Global P/D VLLM arguments (e.g., `--block-size`, `--kv-transfer-config`)
  - **Environment Variables**: Global P/D environment variables (e.g., `VLLM_NIXL_SIDE_CHANNEL_HOST`)
  - **Resource Requirements**: Global P/D resource specifications (e.g., `rdma/ib: '1'`)

### Enhanced VLLM Arguments System
- **Configuration Rename**: Changed `vllm_args` → `vllm_extra_args` for clarity
- **Conditional Behavior**: Different VLLM arg handling based on `use_kserve_defaults` flag
  - **KServe Mode**: Only extra args when `use_kserve_defaults: true`
  - **Standard Mode**: Extra args + automatic `--tensor-parallel-size` when `use_kserve_defaults: false`
- **P/D Integration**: P/D deployments receive both profile args and global P/D args with correct tensor parallelism

### Resource Allocation Improvements  
- **Per-Pod-Type Configuration**: Different resource allocation for prefill vs decode pods
  - **Prefill Pods**: Use prefill tensor parallelism (extracted from `p.tp{N}`)
  - **Decode Pods**: Use main tensor parallelism (extracted from `d.tp{N}` or `tp{N}`)
- **Resource Merging**: Automatic merging of base resources with P/D-specific resources

### Testing Infrastructure Enhancements
- **Annotation-Based Profile Detection**: Added `forge.openshift.io/deployment-profile` annotation for reliable profile identification
  - **Replaced Heuristics**: Eliminated complex directory name parsing in test system
  - **Reliable Testing**: Tests now read profile names directly from manifest annotations
- **Namespace Configuration**: Fixed namespace override behavior for consistent testing

### Environment and Artifact Management
- **Artifact Directory Naming**: Enhanced artifact directory creation with conflict resolution
  - **Base Format**: `forge_YYYYMMDD-HHMM` without seconds when possible
  - **Conflict Resolution**: Add seconds (`forge_YYYYMMDD-HHMM-SS`) when directory exists
- **Symlink Management**: Automatic `/tmp/forge_last` symlink to latest artifact directory

### Files Modified
- `projects/llm_d/orchestration/runtime_config.py` - FROM_NAME extraction system, template resolution, parameter parsing
- `projects/llm_d/orchestration/render_inference_service.py` - P/D rendering, VLLM args system, resource allocation, annotations
- `projects/llm_d/orchestration/config.d/deployments.yaml` - Template profiles, global P/D configuration
- `projects/llm_d/orchestration/config.d/runtime.yaml` - Namespace override, kueue configuration updates
- `projects/llm_d/tests/test_deployment_profiles.py` - Annotation-based profile detection, namespace override
- `projects/core/library/env.py` - Artifact directory conflict resolution, symlink management
- `projects/llm_d/docs/PD_TESTING.md` - Updated documentation for new P/D configuration system

### Benefits
- **Flexible Configuration**: Profile names encode all P/D parameters for easy testing
- **Template Reuse**: Single template profile supports multiple P/D configurations  
- **Resource Optimization**: Different GPU allocation for prefill vs decode workloads
- **Simplified Testing**: Annotation-based profile detection eliminates complex heuristics
- **Clean Architecture**: Global P/D configuration reduces duplication across profiles

## 2026-07-16 - Preset Configuration Support

### Configuration Management
- **Preset CLI Option**: Added `--preset` flag to CI entrypoint for preset configuration before execution
  - **Multiple Presets**: Supports multiple preset values via repeated `--preset` flags
  - **Early Initialization**: Sets preset configuration during environment initialization, before config loading
  - **Variables Override Integration**: Uses framework helper to generate proper override files

### Files Modified
- `projects/llm_d/orchestration/ci.py` - Added `--presets` CLI option and initialization logic

### Benefits
- **Flexible Testing**: Enables running same test suite with different preset configurations
- **Command-Line Control**: Provides runtime preset selection without modifying configuration files
- **Framework Integration**: Leverages core configuration override system for consistent behavior

## 2026-06-26 - Orchestration & Cleanup Improvements

### New Features
- **Preflight Phase**: New orchestration phase for pre-execution validation
  - **Purpose**: Validate environment and prerequisites before resource provisioning
  - **Integration**: Added to CI pipeline for early failure detection

### Enhanced Cleanup System
- **Improved Resource Cleanup**: Enhanced cleanup phase with comprehensive operator and resource management
  - **Resource Targeting**: More precise cleanup of test resources and namespaces
  - **Configuration**: Added cleanup behavior configuration in `config.yaml`

### LeaderWorkerSet Integration
- **CRD Management**: Added proper LeaderWorkerSet CRD waiting and validation
  - **Platform Config**: Added LWS operator configuration to platform settings
  - **Manifest**: Added LeaderWorkerSetOperator manifest template
  - **Synchronization**: Ensures CRDs are available before proceeding with LWS operations

### Test Organization
- **Test Phase Restructuring**: Reorganized test phase to include finalizers within test directory structure
  - **Better Organization**: Test finalizers now properly contained within test artifact structure
  - **Improved Cleanup**: More logical separation between test execution and cleanup operations

### Configuration Enhancements
- **Model Vault Request**: Added model vault configuration to orchestration config
- **Config Review Agent**: Automated config review agent now triggered after initialization
  - **Early Validation**: Catches configuration issues early in the process
  - **Agent Integration**: Leverages agentic capabilities for configuration validation

### Files Modified
- `projects/llm_d/orchestration/ci.py` - Preflight phase integration and config review trigger
- `projects/llm_d/orchestration/preflight_phase.py` - New preflight validation phase (NEW)
- `projects/llm_d/orchestration/cleanup_phase.py` - Enhanced cleanup capabilities  
- `projects/llm_d/orchestration/prepare_phase.py` - LWS CRD waiting logic
- `projects/llm_d/orchestration/test_phase.py` - Reorganized test directory structure
- `projects/llm_d/orchestration/config.yaml` - Model vault and cleanup configuration
- `projects/llm_d/orchestration/config.d/platform.yaml` - LWS operator platform config
- `projects/llm_d/orchestration/manifests/leaderworkersetoperator.yaml` - LWS operator manifest (NEW)

### Benefits
- **Early Validation**: Preflight phase catches issues before expensive resource provisioning  
- **Reliable Cleanup**: Enhanced cleanup system ensures thorough resource removal
- **Better Organization**: Improved test structure and artifact management
- **Automated Review**: Config validation through intelligent agents
- **LWS Support**: Proper LeaderWorkerSet integration with CRD management

## 2026-06-24 - Agentic Configuration & Context Persistence

### Changes
- **Agentic Agents Enabled**: Activated `on_failure` and `config_review` agents for automated analysis

### Active Configuration
```yaml
agentic:
  enabled: true
  model_key: default
  on_failure:
    enabled: true
```

