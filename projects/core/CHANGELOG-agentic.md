# Agentic Framework Changelog

## 2026-06-24 - Configurable Agentic Processing

### New Features
- **Agentic Agent Review System**: Automated failure analysis and config review using LLM
  - **On-Failure Agent**: Automatically analyzes test failures when CI commands fail
  - **Config Review Agent**: Reviews configuration changes for potential issues
- **Configurable Models**: Select AI models and enable/disable agents per environment
- **CLI Model Override**: `--model-key` parameter for failure analysis

### Configuration
```yaml
agentic:
  enabled: true                    # Enable agent review system
  model_key: qwen-3-6-35b         # AI model for analysis
  on_failure:
    enabled: true                  # Auto-analyze test failures
  config_review:
    enabled: true                  # Review configuration changes
```

### Benefits
- **Automated Failure Analysis**: AI automatically investigates test failures and provides root cause analysis
- **Proactive Config Review**: Catch configuration issues before deployment

### Programmatic Usage

**On-Failure Agent Decorator**:
```python
@agent_review_on_failure
def test(ctx) -> int:
    return test_toolbox_run()
```

**Config Review Trigger**:
```python
trigger_config_review_for_ci(env.BASE_ARTIFACT_DIR, async_mode=True)
```

### CLI Usage
```bash
# Use configured model
python projects/core/agentic/on_failure/cli.py --base-artifact-dir /path/to/artifacts

# Override model
python projects/core/agentic/on_failure/cli.py --model-key custom-model --base-artifact-dir /path/to/artifacts
```
