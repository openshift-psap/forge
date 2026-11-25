# Caliper Postprocess Status System

## 🎯 **Overview**

The Caliper Postprocess Status System provides comprehensive typed status management for Caliper orchestration and notifications. This system replaces untyped dictionaries with typed dataclass models for better type safety, IDE support, and maintainability.

## 📋 **Key Features**

✅ **Complete Type Safety**: All status objects use proper dataclass models with type annotations  
✅ **Unified Status Management**: Both command status and postprocess status use consistent patterns  
✅ **Production-Ready Notifications**: GitHub, Slack, and custom notification support  
✅ **YAML Generation/Parsing**: Type-safe YAML serialization for CI/CD integration  
✅ **Rich API**: Helper methods for common status operations  
✅ **Pattern Matching**: Modern Python pattern matching support  
✅ **CI/CD Integration**: Complete examples for all major CI systems  

## 🏗️ **Architecture**

```
┌─────────────────────────┐
│ Caliper Engine Layer    │
│ - Returns typed status  │
│ - KpiAnalysisStatus     │
│ - ParseStatus, etc.     │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Orchestration Layer     │
│ - Consumes typed status │
│ - Generates postprocess │
│ - PostprocessStatus     │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ YAML Output            │
│ postprocess_status.yaml │
│ - Clean string values   │
│ - No Python objects    │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Notification Layer      │
│ - GitHub/Slack/Email    │
│ - Type-safe parsing     │
│ - Rich status analysis  │
└─────────────────────────┘
```

## 🔧 **Status Models**

### **Command Status Models**

Each Caliper command has its own typed status model:

```python
from projects.caliper.public import (
    KpiAnalysisStatus,  # caliper kpi analyse-kpis
    ParseStatus,  # caliper parse
    VisualizeStatus,  # caliper visualize
    KpiGenerateStatus,  # caliper kpi generate
    AiDataExportStatus,  # caliper ai-eval-export
    S3ImportStatus,  # caliper s3-import
    S3ExportStatus,  # caliper s3-export
    CsvExportStatus,  # caliper kpi csv-export
)
```

### **Postprocess Status Model**

```python
@dataclass
class PostprocessStatus:
    final_status: FinalPostprocessStatus
    success: bool | str  # Can be bool or "warning"
    base_directory: str
    test_phase: TestPhaseInfo
    steps: list[dict[str, Any]] = field(default_factory=list)

    # Rich API methods
    def is_success(self) -> bool
    def has_regressions(self) -> bool
    def get_failure_reason(self) -> str | None
    def get_step_result(self, step_name: str) -> dict[str, Any] | None
    def classify(self) -> PostprocessClassification
    def is_test_failure(self) -> bool
    def is_postprocess_failure(self) -> bool
    def has_failed_steps(self) -> bool
```

### **Test failure vs post-processing failure**

`final_status` is the single authoritative CI label: `test_failed` takes
priority over everything, so a run whose test failed gets
`success: false` even when every post-processing step succeeded — and
`test_failed` also masks real step failures (test failed AND parse failed is
still labelled `test_failed`).

Consumers that need to distinguish "the test failed" from "the post-processing
failed" use `classify_postprocess_outcome()` (raw dicts, missing keys
tolerated) or `PostprocessStatus.classify()`. The steps are inspected first so
masked step failures are surfaced:

| final_status | steps | success | outcome | test_failed |
|---|---|---|---|---|
| `success` | no failure | `True` | `success` | false |
| `test_failed` | no failed step | `False` | `success` | true |
| `test_failed` | warnings only | `False` | `warning` | true |
| `test_failed` | ≥1 `failed` | `False` | `postprocess_failure` | true |
| `parse_visualize_failed` / `kpi_pipeline_failed` | any | `False` | `postprocess_failure` | false |
| `kpi_pipeline_failed` | warnings only | `"warning"` | `warning` | false |
| `performance_regression` | no failure | `False` | `performance_regression` | false |
| `performance_increase` | no failure | `False` | `performance_increase` | false |
| missing/unknown | no failure | `False` | `postprocess_failure` (conservative) | per test_phase |

Notification semantics derived from the classification:

- ✅ post-processing succeeded (with a `*(test failed)*` note when the test failed)
- ⚠️ warnings only, or performance increase (an improvement is not a failure)
- ❌ post-processing step failure, or performance regression (red CI gate)

A real post-processing failure also raises the FAILURE flag in the export
notification's overall job status (`_check_postprocess_warnings`); a test
failure does not — it is already reported via the step's own exit status.

### **Status Enums**

```python
class StatusLevel(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    WARNING = "warning"
    DISABLED = "disabled"
    REGRESSION_DETECTED = "regression_detected"


class FinalPostprocessStatus(StrEnum):
    SUCCESS = "success"
    TEST_FAILED = "test_failed"
    PARSE_VISUALIZE_FAILED = "parse_visualize_failed"
    KPI_PIPELINE_FAILED = "kpi_pipeline_failed"
    PERFORMANCE_REGRESSION = "performance_regression"
    PERFORMANCE_INCREASE = "performance_increase"
```

## 📝 **Usage Examples**

### **Engine Layer (Producing Status)**

```python
from projects.caliper.public import KpiAnalysisStatus, create_success_status, create_failure_status

def run_kpi_analysis(...) -> tuple[KpiAnalysisStatus, dict | None]:
    try:
        # ... perform analysis ...
        
        if regressions_found:
            return KpiAnalysisStatus(
                status=StatusLevel.REGRESSION_DETECTED,
                success=False,
                regressions_detected=True,
                regression_count=len(regressions),
                exit_code=3,
                completed_at=time.time(),
            ), report
        else:
            return create_success_status(
                KpiAnalysisStatus,
                output_file=str(output_file),
                total_kpis=len(kpis),
            ), report
            
    except Exception as e:
        return create_failure_status(
            KpiAnalysisStatus, 
            error=str(e),
            exit_code=1
        ), None
```

### **Orchestration Layer (YAML Generation)**

```python
def _save_postprocess_status_yaml(self, result: dict[str, Any]) -> None:
    from projects.caliper.public import PostprocessStatus, save_postprocess_status_yaml
    
    # Convert orchestration result to typed status
    status = PostprocessStatus.from_orchestration_result(result)
    
    # Save as typed YAML
    save_postprocess_status_yaml(status, self.output_dir / "postprocess_status.yaml")
```

### **Notification Layer (Consuming Status)**

```python
from projects.caliper.public import load_postprocess_status_yaml, FinalPostprocessStatus

# Load typed status from YAML
status = load_postprocess_status_yaml(Path("postprocess_status.yaml"))

# Type-safe access with pattern matching
match status.final_status:
    case FinalPostprocessStatus.SUCCESS:
        send_success_notification(status)
    case FinalPostprocessStatus.PERFORMANCE_REGRESSION:
        send_regression_alert(status)
    case _:
        send_failure_notification(status)

# Rich API usage
if status.has_regressions():
    analysis_step = status.get_step_result("analyse_kpis")
    if analysis_step:
        regression_count = analysis_step.get("regression_count", 0)
        total_kpis = analysis_step.get("total_kpis", 0)
        print(f"🚨 {regression_count}/{total_kpis} KPIs regressed")
```

## 🔔 **Notification System**

### **Production-Ready Notifier**

```bash
# Environment configuration
export CALIPER_GITHUB_NOTIFICATIONS=true
export GITHUB_TOKEN=ghp_your_token_here
export GITHUB_REPOSITORY=owner/repo
export GITHUB_PR_NUMBER=123

export CALIPER_SLACK_NOTIFICATIONS=true  
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Run notifications
python -m projects.caliper.notifications /path/to/postprocess_status.yaml
```

### **CI/CD Integration Examples**

#### **GitHub Actions**
```yaml
- name: Send notifications
  if: always()
  env:
    CALIPER_GITHUB_NOTIFICATIONS: true
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    GITHUB_REPOSITORY: ${{ github.repository }}
    GITHUB_PR_NUMBER: ${{ github.event.number }}
  run: |
    python -m projects.caliper.notifications ./results/postprocess_status.yaml
```

#### **Jenkins**  
```groovy
post {
    always {
        script {
            env.CALIPER_SLACK_NOTIFICATIONS = 'true'
            env.SLACK_WEBHOOK_URL = credentials('slack-webhook')
            sh 'python -m projects.caliper.notifications ./results/postprocess_status.yaml'
        }
    }
}
```

#### **GitLab CI**
```yaml
notify_results:
  script:
    - python -m projects.caliper.notifications ./results/postprocess_status.yaml
  variables:
    SLACK_WEBHOOK_URL: $SLACK_WEBHOOK
  when: always
```

## 📊 **Generated YAML Structure**

The system generates clean, readable YAML files:

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
      regressions_detected: false
  - visualize:
      status: "success"  
      completed_at: 1234567892.0
      output_dir: "plots/"
      generated_files: 5
```

**Key Benefits:**
- ✅ **No Python Objects**: Clean string values, no `!!python/object` references
- ✅ **Human Readable**: Easy to inspect and debug
- ✅ **Tool Compatible**: Works with any YAML parser
- ✅ **Version Controlled**: Can be committed and tracked

## 🧪 **Testing**

### **Comprehensive Test Suite**

```bash
# Run all status model tests
python -m projects.caliper.tests.test_kpi_analyze
python -m projects.caliper.tests.test_postprocess_integration

# Test notifications with sample data
python -c "
from projects.caliper.public import *
status = PostprocessStatus(
    final_status=FinalPostprocessStatus.SUCCESS,
    success=True,
    base_directory='/test',
    test_phase=TestPhaseInfo(phase=TestPhase.SUCCESS),
    steps=[]
)
save_postprocess_status_yaml(status, Path('test_status.yaml'))
print('Test file created: test_status.yaml')
"

python -m projects.caliper.notifications test_status.yaml
```

### **Integration Verification**

```python
# Verify public API
from projects.caliper.public import *

print("✅ Status models imported successfully")

# Test YAML roundtrip
status = PostprocessStatus(
    final_status=FinalPostprocessStatus.SUCCESS,
    success=True,
    base_directory="/test",
    test_phase=TestPhaseInfo(phase=TestPhase.SUCCESS),
    steps=[],
)

save_postprocess_status_yaml(status, Path("test.yaml"))
loaded = load_postprocess_status_yaml(Path("test.yaml"))
assert status.final_status == loaded.final_status
print("✅ YAML roundtrip successful")
```

## 🚀 **Migration Guide**

### **From Untyped Dicts to Typed Status**

#### **Engine Functions**
```python
# Before
def analyze_kpis(...) -> dict[str, Any]:
    return {
        "status": "success",
        "exit_code": 0,
        "output_file": str(output_file),
    }

# After  
def analyze_kpis(...) -> KpiAnalysisStatus:
    return create_success_status(
        KpiAnalysisStatus,
        output_file=str(output_file)
    )
```

#### **Orchestration Consumption**
```python
# Before
if result_dict["status"] == "success":
    print(f"Analysis completed: {result_dict['total_kpis']} KPIs")

# After
if status.status == StatusLevel.SUCCESS:
    print(f"Analysis completed: {status.total_kpis} KPIs")
```

#### **Notification Systems**
```python
# Before (manual dict parsing)
with open("postprocess_status.yaml") as f:
    data = yaml.safe_load(f)
    if data["final_status"] == "performance_regression":
        send_alert()

# After (typed parsing)
status = load_postprocess_status_yaml(Path("postprocess_status.yaml"))
if status.final_status == FinalPostprocessStatus.PERFORMANCE_REGRESSION:
    send_alert()
```

## 🎉 **Benefits Summary**

### **For Developers**
- 🔍 **IDE Support**: Full autocompletion and type checking
- 🛡️ **Type Safety**: Catch errors at development time
- 📖 **Self-Documenting**: Clear field types and structure
- 🔄 **Refactoring**: Safe renames and structure changes

### **For Operations**  
- 🔔 **Rich Notifications**: GitHub, Slack, email integration
- 🔧 **CI/CD Ready**: Examples for all major CI systems
- 📊 **Status Tracking**: Comprehensive failure analysis
- 🎯 **Pattern Matching**: Modern Python notification logic

### **For Maintainers**
- 🏗️ **Consistent Architecture**: Unified status patterns
- ✅ **Comprehensive Testing**: Full test coverage
- 📝 **Clear Documentation**: Usage examples and migration guides
- 🔒 **Production Ready**: No fallbacks, fail-fast design

## 📚 **Documentation Files**

- **[STATUS_MODELS_README.md](projects/caliper/public/STATUS_MODELS_README.md)**: Command status models
- **[POSTPROCESS_MODELS_README.md](projects/caliper/public/POSTPROCESS_MODELS_README.md)**: Postprocess status models  
- **[ci_examples.md](projects/caliper/notifications/ci_examples.md)**: CI/CD integration examples
- **[notification_example.py](projects/caliper/public/notification_example.py)**: Notification code examples

## 🎯 **Quick Start**

```bash
# 1. Import the public API
from projects.caliper.public import *

# 2. Generate status (engine layer)
status = create_success_status(KpiAnalysisStatus, total_kpis=10)

# 3. Create postprocess status (orchestration layer) 
result = {...}  # orchestration result dict
postprocess_status = PostprocessStatus.from_orchestration_result(result)
save_postprocess_status_yaml(postprocess_status, Path("status.yaml"))

# 4. Set up notifications (CI/CD layer)
export CALIPER_GITHUB_NOTIFICATIONS=true
export GITHUB_TOKEN=your_token
python -m projects.caliper.notifications status.yaml
```

The Caliper Postprocess Status System provides a complete, production-ready foundation for type-safe status management and notifications! 🚀
