# FORGE CI Orchestration System

`projects/core/ci_entrypoint/run_ci.py` and
`projects/core/ci_entrypoint/prepare_ci.py` are CI wrappers that
provide a unified entrypoint for CI operations across all projects in
the FORGE test harness. They help having simple entrypoints to define
the CI workflows and ensure that the environment is correctly setup to
run tests launched from OpenShift CI and FOURNOS.

## Overview

The system follows the constitutional principle of CI-First Testing by
providing consistent, reliable CI integration across all projects.

- **run_ci.py**: Main entrypoint script offering a unified CLI interface for CI operations
- **prepare_ci.py**: Handles environment setup, validation, GitHub PR integration, and post-execution tasks

## Usage

### Basic Commands

```bash
# List all available projects
./run_ci.py

# List projects explicitly
./run_ci.py projects

# Execute a project operation
./run_ci.py <project> <operation> [args...]

# Show available operations for a project
./run_ci.py <project>
```

### Examples

```bash
./run_ci.py llm_d ci prepare
./run_ci.py llm_d ci test
./run_ci.py skeleton ci validate
```

## Key Features

### Project Discovery and Execution

The system automatically discovers projects by scanning the `projects/` directory for subdirectories containing an `orchestration/` folder. For each operation, it looks for CI scripts in:

- `<project_dir>/orchestration/<operation>.py`

Scripts must be executable Python files that follow Click CLI conventions and must be located in the project's `orchestration/` directory.

### Environment Setup and Validation

**prepare_ci.py** performs comprehensive environment setup:

- **Artifact Directory Management**: Creates and validates `$ARTIFACT_DIR` for storing CI artifacts
- **Dependency Installation**: Automatically installs required packages (`fire`, `click`) using `uv` or `pip`
- **Tool Validation**: Ensures required tools (`jq`, `oc` for full images) are available
- **Environment Variables**: Sets up `FORGE_HOME` and other necessary variables

### Dual Output System

A sophisticated logging system that simultaneously outputs to:
- Console (for real-time monitoring)
- Log file (`$ARTIFACT_DIR/run.log` for persistence)

Uses background threading for efficient I/O handling with proper cleanup and buffer flushing.

### GitHub PR Integration

When running in a GitHub PR context, the system:

- Parses PR arguments from environment variables (`REPO_OWNER`, `REPO_NAME`, `PULL_NUMBER`)
- Downloads PR metadata and comments via GitHub API
- Extracts test directives from PR descriptions
- Saves configuration overrides to `$ARTIFACT_DIR/_meta/variable_overrides.yaml`
- Stores PR directives in `$ARTIFACT_DIR/_meta/pr_config.txt`

### Git Version Tracking

Automatically captures and stores:
- FORGE repository git version (`_meta/forge.git_version`)
- Commit history context for debugging

### Signal Handling

Graceful handling of interruption signals:
- **SIGINT (Ctrl+C)**: Exit code 130 with emergency cleanup
- **SIGTERM**: Exit code 143 with emergency cleanup
- **SIGPIPE**: Default handling for broken pipes

Emergency cleanup includes flushing buffers and terminating background threads.

### Error Handling and Status Reporting

**Pre-execution Checks**:
- Validates artifact directory accessibility
- Checks for existing failure markers (unless `FORGE_IGNORE_FAILURES_FILE=1`)
- Verifies required tools and environment variables

**Post-execution Processing**:
- Consolidates all `FAILURE` files into `$ARTIFACT_DIR/FAILURES`
- Tracks execution duration with human-readable formatting
- Sends notifications via GitHub and Slack (configurable)

### Argument Processing

Automatic conversion of underscore arguments to hyphens for Click compatibility:
- `my_arg_name` → `my-arg-name`
- Conversion details shown in verbose output

## Artifact Structure

When `ARTIFACT_DIR` is set, creates the following structure:

```
$ARTIFACT_DIR/
├── run.log                           # Complete execution log
├── FAILURES                          # Consolidated failure info (on failure)
└── _meta/                           # Metadata directory
    ├── variable_overrides.yaml     # PR configuration overrides
    ├── pr_config.txt               # PR test directives
    ├── pull_request.json           # PR metadata from GitHub API
    ├── pull_request-comments.json  # PR comments from GitHub API
    └── forge.git_version           # FORGE git version info
```

## Environment Variables

### Core Variables
- `ARTIFACT_DIR`: Directory for CI artifacts (auto-created in local development)
- `FORGE_HOME`: Root directory of FORGE repository
- `OPENSHIFT_CI`: Set to `'true'` when running in OpenShift CI
- `FORGE_LIGHT_IMAGE`: Set when using lightweight container images

### GitHub PR Variables
- `REPO_OWNER`: GitHub repository owner (default: `openshift-psap`)
- `REPO_NAME`: GitHub repository name (default: `forge`)
- `PULL_NUMBER`: PR number for GitHub integration
- `PULL_BASE_SHA`, `PULL_PULL_SHA`: Base and head commit SHAs
- `TEST_NAME`: Name of the test being executed
- `SHARED_DIR`: Directory for shared test artifacts

### Configuration Variables
- `FORGE_IGNORE_FAILURES_FILE`: Set to `'1'` to ignore existing failure markers
- `FORGE_NOTIFICATION_DRY_RUN`: Set to `'true'` for dry-run notifications

## Integration with CI Systems

### OpenShift CI Integration

Seamless integration with OpenShift CI including:
- Automatic artifact directory handling
- Step directory configuration via `HOSTNAME` and `JOB_NAME_SAFE`
- GitHub status reporting and PR comment integration
- Notification system for build status

### FOURNOS Integration

Compatible with FOURNOS test orchestration system with special handling:
- Project aliasing: `forge` → `fournos`
- Environment-specific configuration
- Artifact management for distributed testing

## Notification System

Sends notifications through multiple channels:

**GitHub**: Status updates on pull requests
**Slack**: Team notifications (when configured)

**Logic**:
- Success notifications only for `test` and `submit` operations
- Failure notifications for all operations
- Dry-run mode available for testing
- Skipped for `jump_ci` project

## Troubleshooting

### Common Issues

1. **Project Not Found**: Ensure project has `orchestration/` directory and proper structure
2. **Operation Not Found**: Check script exists and is executable (`chmod +x`)
3. **Environment Issues**: Verify `ARTIFACT_DIR` permissions and required tools
4. **Package Installation Failures**: Check network connectivity and Python path

### Debugging

- **Complete Logs**: `$ARTIFACT_DIR/run.log` contains all output
- **Failure Analysis**: `$ARTIFACT_DIR/FAILURES` consolidates error information
- **Verbose Mode**: Use `--verbose` for detailed execution information
- **Dry Run**: Use `--dry-run` to see execution plan without running

The entrypoint wrappers add comprehensive log headers and footers to help understand how tests are executed, make failures clear, and provide integration with GitHub and Slack notification systems for CI/CD workflows.
