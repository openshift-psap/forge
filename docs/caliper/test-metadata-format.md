# Test Labels Format (`__caliper_test_metadata__.yaml`)

## Purpose

The `__caliper_test_metadata__.yaml` file marks directories as Caliper test bases and provides metadata for test identification, grouping, and status tracking.

## File Format

```yaml
version: "1"
skip: true          # optional — omit this directory from all Caliper processing
labels:
  # Test distinguishing characteristics  
  key1: value1
  key2: value2
kpi_labels:
  # System/environment context for KPI labeling
  platform: "CKS"
  gpu_type: "H100"
  test_harness: "guidellm"
completion:
  # Test execution status (added at completion)
  success: true|false
  message: "Status description"
```

### Required Fields
- **`version`**: Schema version (currently `"1"`)
- **`labels`**: Key-value pairs describing test characteristics

### Optional Fields
- **`skip`**: Set to `true` to exclude this directory from all Caliper discovery, filtering, and report generation. Useful for marking in-progress, broken, or intentionally ignored test results. Can be placed at the top level of the file (preferred) or inside the `labels:` section — both are supported.
- **`kpi_labels`**: System/environment context labels for KPI records
  - **`platform`**: Platform name (e.g., `"CKS"`, `"RHOAI"`)
  - **`gpu_type`**: GPU type (e.g., `"H100"`, `"A100"`)
  - **`test_harness`**: Test framework (e.g., `"guidellm"`, `"vllm"`)
- **`mlflow_destination`** *(optional)*: Pre-created MLflow run for artifact upload
  - **`run_id`**: MLflow run ID (assigned by the server during pre-creation)
  - **`experiment_id`** *(optional)*: MLflow experiment ID
  - **`workspace`** *(optional)*: MLflow workspace name
- **`completion`**: Test execution status
  - **`success`**: `true` if succeeded, `false` if failed
  - **`message`**: Human-readable status description

## Labels vs KPI Labels

### Labels (Test Configuration)
- **Purpose**: Distinguish different test configurations and variations
- **Usage**: Used for test filtering, grouping, and analysis
- **Examples**: `model_name`, `deployment_profile`, `guidellm_loadshape`
- **Scope**: Specific to the test configuration

### KPI Labels (System Context)
- **Purpose**: Provide system/environment context for KPI records
- **Usage**: Applied to all KPI metrics from the test, enables cross-environment analysis
- **Examples**: `platform`, `gpu_type`, `test_harness`
- **Scope**: Describes the execution environment

## Common Labels

- **`model_name`**: AI/ML model name (e.g., `"llama-3.1-8b"`)
- **`deployment_profile`**: Deployment variant (e.g., `"simple-tp2-x4"`)  
- **`guidellm_loadshape`**: Benchmark load pattern (e.g., `"heavy-heterogeneous"`)
- **`benchmark_type`**: Benchmark type (e.g., `"throughput"`, `"latency"`)

## Common KPI Labels

- **`platform`**: Target platform (e.g., `"CKS"`, `"RHOAI"`)
- **`gpu_type`**: GPU hardware type (e.g., `"H100"`, `"A100"`)
- **`test_harness`**: Testing framework (e.g., `"guidellm"`, `"vllm"`)

## Examples

### Basic Test
```yaml
version: "1"
labels:
  model_name: "llama-3.1-8b"
  deployment_profile: "simple"
  guidellm_loadshape: "default"
kpi_labels:
  platform: "CKS"
  gpu_type: "H100"
  test_harness: "guidellm"
```

### Completed Test
```yaml
version: "1"
labels:
  model_name: "llama-3.1-8b"
  deployment_profile: "simple-tp2-x4"
  guidellm_loadshape: "heavy-heterogeneous"
kpi_labels:
  platform: "CKS"
  gpu_type: "H100"
  test_harness: "guidellm"
mlflow_destination:
  run_id: "48e49dfc966c487cb76cf105a5314908"
  experiment_id: "264"
  workspace: "forge-rhaiis"
completion:
  success: true
  message: "Test completed successfully"
```

### Failed Test
```yaml
version: "1"
labels:
  model_name: "gpt-4o"
  deployment_profile: "distributed"
kpi_labels:
  platform: "RHOAI"
  gpu_type: "A100"
  test_harness: "vllm"
completion:
  success: false
  message: "Connection timeout to inference service"
```

### Skipped Test
```yaml
version: "1"
skip: true
labels:
  model_name: "llama-3.1-8b"
  deployment_profile: "simple"
```

Caliper will not discover or process this directory at all. The `skip` field can also be placed inside the `labels:` section for compatibility with older tooling, but top-level placement is preferred.

## Usage

- **Test Discovery**: Caliper scans for `__caliper_test_metadata__.yaml` files to find test results
- **Test Analysis**: Labels enable filtering and comparative analysis of test configurations
- **KPI Labeling**: KPI labels are automatically applied to all generated KPI metrics
- **Cross-Environment Analysis**: KPI labels enable performance comparisons across different platforms/hardware
- **Report Generation**: Both labels and KPI labels provide context for charts and report organization
- **Status Tracking**: Completion field tracks test execution outcome

## Best Practices

- **Labels**: Use for test-specific configuration and distinguishing characteristics
- **KPI Labels**: Use for system/environment context that applies to all KPIs
- Generate both label sets early, add completion status at test end
- Use consistent, descriptive keys across related tests
- Use lowercase with underscores (e.g., `model_name`, `gpu_type`)
- Keep label schemas stable within projects
- Populate KPI labels from configuration to ensure consistency (e.g., from `cpt.kpi.labels.*`)
- Missing KPI labels should be handled gracefully with fallback values
