# DSL Framework Changelog

## 2026-06-26 - Early Return & Enhanced Logging

### New Features
- **Early Return Capability**: Tasks can now return `EarlyReturn` to gracefully terminate execution
  - **Behavior**: Stops executing remaining non-@always tasks while preserving cleanup tasks
  - **Usage**: `return EarlyReturn("reason for early exit")`
  - **Benefits**: Allows conditional early exit (e.g., operator already deployed) without failure status

### Changed  
- **Enhanced Retry Logging**: Improved retry attempt logging with artifact directory context
  - **Context Display**: Retry headers now show artifact directory suffix for better identification
  - **Better Tracking**: Enhanced visibility into which toolbox/operation is retrying
  - **Consistent Formatting**: Unified retry message format across all DSL operations

### Control Flow
```python
from projects.core.dsl import EarlyReturn

@task
def check_operator_deployed(args, ctx):
    if operator_exists():
        return EarlyReturn("Operator already deployed, skipping installation")
    # Continue with installation...
```

### Files Modified
- `projects/core/dsl/__init__.py` - Exported `EarlyReturn` class
- `projects/core/dsl/control_flow.py` - New `EarlyReturn` and `EarlyReturnException` classes (NEW)
- `projects/core/dsl/runtime.py` - Early return handling and exception propagation
- `projects/core/dsl/task.py` - Enhanced retry logging with artifact directory context

### Benefits
- **Graceful Early Exit**: Clean termination when conditions are already met
- **Preserved Cleanup**: @always tasks still execute after early return
- **Better Debugging**: Enhanced retry logging helps identify which operations are struggling
- **Improved UX**: Clear messaging when operations complete early vs. when they fail

## 2026-06-24 - Context Persistence & Logging Improvements

### New Features
- **Context Persistence**: DSL runtime automatically saves final shared context on successful completion
  - **Location**: `{artifact_dir}/_meta/context.yaml`
  - **Content**: All context variables set by tasks during execution
  - **Format**: Clean YAML with timestamp and Path objects converted to strings

### Changed
- **Reduced Logging Verbosity**: Removed context output from exception traces
  - Context no longer printed to console on task failures
  - Exception traces now show full stack traces for better debugging
  - Context still preserved in `context.yaml` on successful completion

### New Artifacts
```
{artifact_dir}/
└── _meta/
    ├── metadata.yaml           # Execution metadata
    ├── restart.sh             # Restart script
    ├── env.txt                # Environment variables
    └── context.yaml           # Final task context (NEW)
```

### Context File Format
```yaml
final_context:
  cache_spec:
    source_uri: "hf://openai/gpt-oss-120b"
    pvc_name: "llm-d-model-openai-gpt-oss-120b-7ab79ddecd"
  artifact_dir: "/path/to/artifacts"
timestamp: "2026-06-24T10:30:45.123456"
```

### Benefits
- **Better Debugging**: Inspect final task state and variable values
- **Cleaner Console Output**: Reduced verbosity while preserving diagnostic information
- **Context Tracking**: See what values tasks set during execution
- **Automatic**: No configuration required, works for all DSL executions

### Files Modified
- `projects/core/dsl/runtime.py` - Added `_generate_context_file()` function
- `projects/core/dsl/toolbox.py` - Removed context logging from exception handler

### Migration Notes
- **No Breaking Changes**: All existing DSL functionality preserved
- **Automatic Benefits**: Context persistence activates immediately for all projects
- **Backward Compatible**: Existing toolbox commands continue to work unchanged
