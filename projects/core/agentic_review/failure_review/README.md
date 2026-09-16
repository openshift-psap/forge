# FORGE Failure Review Agent (NOOA Implementation)

This module provides AI-powered analysis of FORGE test failures using the NVIDIA Object-Oriented Agents (NOOA) framework.

## Overview

The new failure review agent represents a significant architectural improvement over the previous `on_failure` module:

### Key Improvements

- **Object-Oriented Design**: Agents are Python classes with typed state and capabilities
- **Structured Outputs**: Type-safe analysis results using dataclasses
- **Better State Management**: Context preserved across analysis steps
- **Enhanced File Investigation**: Intelligent artifact file analysis
- **Simplified Architecture**: Cleaner separation between orchestration and analysis logic

```python
from projects.core.agentic_review.review.failure_review import FailureReviewAgent, FailureContext
from projects.core.agentic_review.review.models import create_llm_client, load_model_config

# Direct agent usage - no compatibility wrappers
models_config = load_model_config("psap-models-corp-rh", "agent-models.yaml")
llm = create_llm_client(models_config["qwen-3-6-35b"])
agent = FailureReviewAgent(llm=llm)

context = FailureContext(...)
analysis = agent.analyze_failure(context)  # Returns typed FailureAnalysis object
```

## Installation

### Prerequisites

Install the NOOA package:

```bash
pip install nooa
```

### FORGE Configuration

Update your FORGE configuration to enable the new agent:

```yaml
# config/agentic.yaml
agentic:
  enabled: true
  failure_review:
    enabled: true
  model_key: "qwen-3-6-35b"
```

## Usage

### CLI Usage

```bash
# Basic usage
forge failure-review --base-artifact-dir /path/to/artifacts

# JSON output
forge failure-review --base-artifact-dir /path/to/artifacts --output-format json

# Verbose analysis
forge failure-review --base-artifact-dir /path/to/artifacts --verbose

# Different model
forge failure-review --base-artifact-dir /path/to/artifacts --model-key gpt-4
```

### Programmatic Usage

```python
from pathlib import Path
from projects.core.agentic_review.failure_review import FailureReviewAgent, FailureContext
from projects.core.agentic_review.review.models import create_llm_client, load_model_config
from projects.core.agentic_review.artifact_processing import find_failure_files, read_failure_and_log

# Load model configuration
models_config = load_model_config("psap-models-corp-rh", "agent-models.yaml")
model_config = models_config["qwen-3-6-35b"]

# Create NOOA client and agent
llm = create_llm_client(model_config)
agent = FailureReviewAgent(llm=llm)

# Find and analyze failures
base_artifact_dir = Path("/path/to/artifacts")
failure_files = find_failure_files(base_artifact_dir)

for failure_file in failure_files:
    # Read failure data
    failure_data = read_failure_and_log(failure_file)

    # Create context
    context = FailureContext(
        failure_file=failure_file,
        base_artifact_dir=base_artifact_dir,
        failure_content=failure_data["failure_content"],
        log_content=failure_data["log_content"],
        agent_md_content=failure_data.get("agent_md_content", ""),
        available_files=[],  # List of available files
    )

    # Analyze with NOOA agent - returns typed FailureAnalysis object
    analysis = agent.analyze_failure(context, verbose=True)

    print(f"Root Cause: {analysis.root_cause}")
    print(f"Severity: {analysis.severity}")
    print(f"Category: {analysis.category}")
    print(f"Technical Details: {analysis.technical_details}")
```

### Batch Analysis

```python
def analyze_all_failures(base_artifact_dir: Path, model_key: str = "qwen-3-6-35b"):
    """Analyze all failures in an artifact directory using NOOA agents"""

    # Setup
    models_config = load_model_config("psap-models-corp-rh", "agent-models.yaml")
    llm = create_llm_client(models_config[model_key])
    agent = FailureReviewAgent(llm=llm)

    # Find failures
    failure_files = find_failure_files(base_artifact_dir)
    analyses = []

    # Analyze each failure
    for failure_file in failure_files:
        failure_data = read_failure_and_log(failure_file)
        context = FailureContext(
            failure_file=failure_file,
            base_artifact_dir=base_artifact_dir,
            failure_content=failure_data["failure_content"],
            log_content=failure_data["log_content"],
            agent_md_content=failure_data.get("agent_md_content", ""),
            available_files=[],
        )

        analysis = agent.analyze_failure(context)
        analyses.append(analysis)

    return analyses
```

## Architecture

### Core Components

1. **FailureReviewAgent**: Main NOOA agent class that orchestrates the analysis
2. **FailureAnalysis**: Structured dataclass for analysis results
3. **FailureContext**: Context dataclass containing all failure information
4. **CLI Module**: Click-based command line interface

### Analysis Workflow

1. **Categorization**: Classify failure type and severity
2. **Root Cause Analysis**: Deep technical investigation
3. **Failed Step Identification**: Pinpoint exact failure location
4. **File Investigation**: Analyze relevant artifact files
5. **Technical Analysis**: Generate comprehensive technical report
6. **Executive Summary**: Create stakeholder-friendly summary

### Agent Methods

The `FailureReviewAgent` uses NOOA's agent methodology:

```python
class FailureReviewAgent(Agent):
    def categorize_failure(self, context: FailureContext) -> str:
        """Categorize the failure type and severity"""
        ...  # NOOA generates and executes analysis code

    def identify_root_cause(self, context: FailureContext) -> str:
        """Perform deep root cause analysis"""
        ...  # NOOA generates investigation code

    # Regular Python method (deterministic)
    def analyze_failure(self, context: FailureContext) -> FailureAnalysis:
        """Orchestrate the complete analysis workflow"""
        # Coordinate analysis steps
        category = self.categorize_failure(context)
        root_cause = self.identify_root_cause(context, category)
        # ... additional steps
        return FailureAnalysis(...)
```

## Output Format

### Structured Analysis

The new implementation provides strongly typed analysis results:

```python
@dataclass
class FailureAnalysis:
    root_cause: str
    failed_step: str
    category: str
    severity: str  # low, medium, high, critical
    summary: str
    technical_details: str
    timeline: Optional[str] = None
    configuration_issues: Optional[str] = None
    recommended_actions: Optional[List[str]] = None
```

### Enhanced JSON Output

```json
{
  "status": "success",
  "agent_type": "nooa",
  "failures_found": 2,
  "successful_analyses": 2,
  "analyses": [
    {
      "failure_dir": "path/to/failure",
      "status": "success",
      "structured_analysis": {
        "root_cause": "Connection timeout to service endpoint",
        "failed_step": "Service health check validation",
        "category": "timeout",
        "severity": "high",
        "summary": "Test failed due to service connectivity issues...",
        "technical_details": "Detailed analysis..."
      },
      "investigated_files": ["deployment.yaml", "service_description.txt"],
      "processing_time": 12.3
    }
  ]
}
```

## Security Considerations

The NOOA framework executes LLM-generated code, which requires proper sandboxing:

- **Development**: Mock implementations used when NOOA unavailable
- **Testing**: Run in isolated containers with restricted file access
- **Production**: Implement file access controls and network restrictions

See the main project documentation for security implementation details.

## Troubleshooting

### NOOA Not Available

If NOOA is not installed, the agent falls back to mock implementations:

```
🤖 NOOA not available - falling back to mock implementation
```

Install NOOA to enable full functionality:

```bash
pip install nooa
```

### Configuration Issues

Ensure your configuration includes the new settings:

```yaml
agentic:
  failure_review:
    enabled: true
```

### Model Configuration

Verify your model configuration in the vault:

```bash
forge vault-manager --vault psap-models-corp-rh --content agent-models.yaml
```

## Development

### Testing

```bash
# Run tests for the failure review module
pytest tests/core/agentic/test_failure_review.py

# Test with real artifacts (requires NOOA)
forge failure-review --base-artifact-dir tests/fixtures/artifacts --verbose
```

### Adding New Analysis Capabilities

To extend the agent with new analysis methods:

1. Add the method to `FailureReviewAgent` using NOOA's `...` syntax
2. Update the `analyze_failure` orchestration method
3. Extend `FailureAnalysis` dataclass if needed
4. Update tests and documentation

## Migration Guide

### For Users

- **Breaking**: CLI interface completely changed - direct NOOA agent usage
- **Breaking**: No wrapper functions - must instantiate agents directly
- **Breaking**: Update configuration keys from `on_failure` to `failure_review`
- **Enhanced**: Structured, typed results instead of dictionaries

### For Developers

- **Complete Rewrite Required**: No compatibility layer exists
- **New Pattern**: Direct agent instantiation instead of function calls:

```python
# OLD (removed)
from projects.core.agentic_review.on_failure import run_on_failure_agent

result = run_on_failure_agent(artifact_dir)

# NEW (required)
from projects.core.agentic_review.failure_review import FailureReviewAgent, FailureContext
from projects.core.agentic_review.review.models import create_llm_client, load_model_config

llm = create_llm_client(model_config)
agent = FailureReviewAgent(llm=llm)
analysis = agent.analyze_failure(context)
```

- **Structured Results**: Use `FailureAnalysis` dataclass instead of dictionaries
- **Type Safety**: All results are strongly typed
- **NOOA Patterns**: Leverage object-oriented agent design

### Complete Migration Required

The old `on_failure` module has been completely removed:

1. **No gradual migration** - complete rewrite required
2. **Install NOOA**: `pip install nooa`
3. **Update all integrations** to use NOOA agents directly
4. **Test thoroughly** - different architecture and results format
