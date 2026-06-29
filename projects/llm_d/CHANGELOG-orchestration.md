# LLM-D Orchestration Changelog

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
  model_key: qwen-3-6-35b
  on_failure:
    enabled: true
```

