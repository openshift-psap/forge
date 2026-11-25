# Caliper Postprocess Status Models

This document describes the typed dataclass models for Caliper postprocess status objects, providing type-safe generation and parsing of `postprocess_status.yaml` files used for notifications.

## Overview

The postprocess status models provide:

- **Type Safety**: Dataclass models with proper typing for postprocess status structure
- **YAML Generation**: Type-safe generation of `postprocess_status.yaml` files
- **YAML Parsing**: Type-safe parsing for notification systems  
- **Notification Support**: Rich API for building notification messages
- **Pattern Matching**: Modern Python pattern matching support

## Public API

All postprocess models are exposed in the `projects.caliper.public` module:

```python
from projects.caliper.public import (
    # Enums
    FinalPostprocessStatus,
    PostprocessTestPhase,
    StepStatus,
    # Models
    PostprocessStatus,
    PostprocessTestPhaseInfo,
    # I/O Functions
    save_postprocess_status_yaml,
    load_postprocess_status_yaml,
)
```

## Status Enums

### FinalPostprocessStatus
Overall postprocess outcome:
```python
class FinalPostprocessStatus(StrEnum):
    SUCCESS = "success"
    TEST_FAILED = "test_failed"
    PARSE_VISUALIZE_FAILED = "parse_visualize_failed"
    KPI_PIPELINE_FAILED = "kpi_pipeline_failed"
    PERFORMANCE_REGRESSION = "performance_regression"
    PERFORMANCE_INCREASE = "performance_increase"
```

### PostprocessTestPhase
Test phase outcomes:
```python
class PostprocessTestPhase(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
```

### StepStatus
Individual step status:
```python
class StepStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    WARNING = "warning"
    DISABLED = "disabled"
    REGRESSION_DETECTED = "regression_detected"
```

## Core Models

### PostprocessStatus
Main postprocess status object:
```python
@dataclass
class PostprocessStatus:
    final_status: FinalPostprocessStatus
    success: Union[bool, str]  # Can be bool or "warning"
    base_directory: str
    test_phase: PostprocessTestPhaseInfo
    steps: List[Dict[str, Any]] = field(default_factory=list)
```

**Key Methods:**
- `is_success() -> bool`: Check if overall status is successful (CI-label semantics: `final_status == success`)
- `has_regressions() -> bool`: Check if regressions were detected
- `get_failure_reason() -> str`: Get human-readable failure reason
- `get_step_result(step_name) -> dict`: Get specific step result
- `to_dict() -> dict`: Convert to dictionary for YAML
- `from_dict(data) -> PostprocessStatus`: Parse from dictionary
- `from_orchestration_result(result) -> PostprocessStatus`: Convert from orchestration
- `classify() -> PostprocessClassification`: Classify post-processing health independently of the test-phase outcome
- `is_test_failure() -> bool`: Check whether the test phase failed (as opposed to the post-processing)
- `is_postprocess_failure() -> bool`: Check whether the post-processing itself failed, independently of the test phase
- `has_failed_steps() -> bool`: Check whether any post-processing step failed

### Test failure vs post-processing failure

`is_success()` answers the CI-label question ("is the overall final status
success?"). A run whose *test* failed but whose post-processing succeeded gets
`final_status: test_failed, success: false` — `is_success()` is correctly
`False` there. To answer the orthogonal question "did the post-processing
itself fail?", use `classify()` / `classify_postprocess_outcome()`:

```python
from projects.caliper.public import classify_postprocess_outcome

classification = classify_postprocess_outcome(status_data)  # raw dict, missing keys tolerated
classification.outcome  # PostprocessOutcome (see below)
classification.test_failed  # bool — did the test phase fail?
```

`PostprocessOutcome` values: `success`, `warning`, `postprocess_failure`,
`performance_regression`, `performance_increase`.

Because `final_status` gives `test_failed` top priority, it can **mask** real
post-processing step failures (test failed AND parse failed → `test_failed`).
The classifier therefore inspects the steps first: any step with
`status: failed` yields `postprocess_failure` even under a `test_failed` label.

### PostprocessTestPhaseInfo
Test phase information:
```python
@dataclass
class PostprocessTestPhaseInfo:
    phase: PostprocessTestPhase
    message: Optional[str] = None
```

## Usage Patterns

### Generation (Orchestration Side)

The orchestration layer automatically uses typed models for YAML generation:

```python
# In postprocess.py - automatic conversion
def _save_postprocess_status_yaml(self, result: dict[str, Any]) -> None:
    from projects.caliper.public import PostprocessStatus, save_postprocess_status_yaml
    
    # Convert dict result to typed status
    status = PostprocessStatus.from_orchestration_result(result)
    
    # Save using typed YAML function
    save_postprocess_status_yaml(status, status_file)
```

### Parsing (Notification Side)

Notification systems can parse YAML files with full type safety:

```python
from projects.caliper.public import load_postprocess_status_yaml, FinalPostprocessStatus

# Load typed status from YAML
status = load_postprocess_status_yaml(Path("postprocess_status.yaml"))

# Type-safe access with IDE completion
if status.final_status == FinalPostprocessStatus.PERFORMANCE_REGRESSION:
    print(f"🚨 Regression detected: {status.has_regressions()}")
    
    # Access specific step data
    analysis_step = status.get_step_result("analyse_kpis")
    if analysis_step:
        regression_count = analysis_step.get("regression_count", 0)
        total_kpis = analysis_step.get("total_kpis", 0)
        print(f"Regressions: {regression_count}/{total_kpis} KPIs")
```

### Pattern Matching

Modern Python pattern matching works seamlessly:

```python
match status.final_status:
    case FinalPostprocessStatus.SUCCESS:
        send_success_notification(status)
    case FinalPostprocessStatus.PERFORMANCE_REGRESSION:
        send_regression_alert(status)
    case FinalPostprocessStatus.TEST_FAILED:
        send_failure_notification(status)
    case _:
        send_generic_notification(status)
```

### Notification Systems

Example GitHub notification handler:

```python
from projects.caliper.public import load_postprocess_status_yaml


class GitHubNotifier:
    def __init__(self, status_file: Path):
        self.status = load_postprocess_status_yaml(status_file)

    def should_notify(self) -> bool:
        return self.status.is_failure() or self.status.has_regressions()

    def create_comment(self) -> str:
        title = self._get_title()
        summary = self._get_summary()
        return f"{title}\n\n{summary}"

    def _get_title(self) -> str:
        match self.status.final_status:
            case FinalPostprocessStatus.SUCCESS:
                return "✅ Caliper postprocess completed successfully"
            case FinalPostprocessStatus.PERFORMANCE_REGRESSION:
                return "🚨 Performance regression detected"
            case _:
                return f"⚠️ Postprocess status: {self.status.final_status}"
```

## YAML Structure

The generated YAML has this structure:

```yaml
final_status: "success"
success: true
base_directory: "/path/to/artifacts"
test_phase:
  phase: "SUCCESS"
  message: null
steps:
  - parse:
      status: "success"
      completed_at: 1234567890.0
      plugin_module: "my.plugin"
      record_count: 42
  - analyse_kpis:
      status: "success"
      completed_at: 1234567891.0
      output_file: "analysis.yaml"
      total_kpis: 10
      regression_count: 0
```

## Step Result Models

While step results are stored as flexible dictionaries in the steps list, common patterns include:

### Parse Step
```python
{
    "status": "success",
    "completed_at": 1234567890.0,
    "plugin_module": "my.plugin",
    "record_count": 42,
    "parse_cache_ref": "abc123",
}
```

### KPI Analysis Step  
```python
{
    "status": "success",
    "completed_at": 1234567891.0,
    "output_file": "analysis.yaml",
    "total_kpis": 10,
    "regression_count": 0,
    "regressions_detected": false,
}
```

### Failed Step
```python
{"status": "failed", "completed_at": 1234567892.0, "error": "Parse failed: invalid JSON"}
```

## Helper Functions

### I/O Functions
```python
# Save typed status to YAML
save_postprocess_status_yaml(status: PostprocessStatus, file_path: Path) -> None

# Load typed status from YAML  
load_postprocess_status_yaml(file_path: Path) -> PostprocessStatus
```

### Convenience Methods
```python
status.is_success()  # True if overall success
status.is_failure()  # True if failed (not warning)
status.has_regressions()  # True if regressions detected
status.get_failure_reason()  # Human-readable failure description
status.get_step_result(name)  # Get specific step result dict
```

## Integration Example

Complete example showing generation and consumption:

```python
# Generation (Orchestration)
from projects.caliper.public import PostprocessStatus, save_postprocess_status_yaml

# Orchestration creates result dict
result = {
    "final_status": "success",
    "success": True,
    "base_directory": "/artifacts",
    "test_phase": {"phase": "SUCCESS", "message": None},
    "steps": [{"parse": {"status": "success", "completed_at": 123456789.0}}],
}

# Convert to typed status and save
status = PostprocessStatus.from_orchestration_result(result)
save_postprocess_status_yaml(status, Path("postprocess_status.yaml"))

# Consumption (Notifications)
from projects.caliper.public import load_postprocess_status_yaml, FinalPostprocessStatus

# Load and use typed status
status = load_postprocess_status_yaml(Path("postprocess_status.yaml"))

if status.final_status == FinalPostprocessStatus.SUCCESS:
    print("✅ All systems go!")
else:
    print(f"⚠️ Issue: {status.get_failure_reason()}")

    # Detailed step analysis
    for step in status.steps:
        for step_name, step_data in step.items():
            status_val = step_data.get("status", "unknown")
            print(f"  {step_name}: {status_val}")
```

## Benefits

1. **Type Safety**: Catch errors at development time with proper typing
2. **IDE Support**: Full autocompletion and refactoring support
3. **Notification-Friendly**: Rich API specifically designed for notification systems
4. **Maintainable**: Self-documenting code with clear field types
5. **Extensible**: Easy to add new fields and methods as needed
6. **Pattern Matching**: Modern Python pattern matching support
7. **Backward Compatible**: Existing YAML structure preserved

## Testing

The models include comprehensive test coverage:

```bash
python -c "from projects.caliper.public import *; print('✅ Import successful')"
```

See `notification_example.py` for complete usage examples with GitHub and Slack integrations.
