# KServe Toolbox Changelog

## 2026-06-26 - Enhanced Logging & Reliability

### prepare_hf_model_cache

#### Enhanced Logging
- **Improved Logging**: Enhanced logging throughout the model cache preparation process
  - **Better Visibility**: More detailed progress reporting during cache operations
  - **Clearer Status**: Enhanced status messages for better troubleshooting
  - **Error Context**: Improved error messaging with more actionable information

#### Files Modified
- `projects/kserve/toolbox/prepare_hf_model_cache/main.py` - Enhanced logging and status reporting

#### Benefits
- **Better Debugging**: Enhanced logging provides clearer insights into cache preparation
- **Improved Reliability**: Better error handling and status reporting

### deploy_llmisvc

#### Optimized Pod Wait Time
- **Faster Feedback**: Reduced pod appearance wait time to 1 minute for faster feedback
  - **Efficiency**: Faster deployment validation and quicker failure detection  
  - **Improved UX**: Reduced wait times in deployment pipeline

#### Files Modified
- `projects/kserve/toolbox/deploy_llmisvc/main.py` - Reduced pod wait timeout

#### Benefits
- **Faster Feedback**: Reduced wait times accelerate deployment validation

## 2026-06-24 - Deployment Improvements & Agent Integration

### deploy_llmisvc

#### Agent Integration
- **Service Description Capture**: Automatically captures LLMISV description for troubleshooting
- **Agent Review Support**: Generates `AGENT.md` files to help failure analysis agents understand deployment issues
- **Post-Mortem Analysis**: `AGENT.md` includes:
  - Service details and deployment context
  - Available artifact files for analysis  
  - Structured analysis instructions for AI agents
  - References to K8s events and ReplicaSet status

#### Files Added
- `projects/kserve/toolbox/deploy_llmisvc/on_failure_helpers.py` - Agent review support (NEW)

#### Files Modified
- `projects/kserve/toolbox/deploy_llmisvc/main.py` - Timeout reduction and description capture

#### Benefits
- **Better Debugging**: Enhanced artifact capture and structured failure context
- **AI-Friendly**: Agent-optimized failure information for automated analysis

#### Artifact Structure
```
{artifact_dir}/
├── AGENT.md                     # Agent analysis context (NEW)
├── artifacts/
│   ├── llmisv_description.txt   # Service K8s description (NEW)  
│   └── replicaset_description.txt
└── task.log
```

### prepare_hf_model_cache

#### Error Handling Improvements
- **Improved Error Handling**: No longer retries on missing job scenarios
- **Better Resource Management**: More efficient handling of job lifecycle

#### Files Modified
- `projects/kserve/toolbox/prepare_hf_model_cache/main.py` - Error handling improvements

#### Benefits
- **More Reliable**: Improved error handling prevents unnecessary retries
