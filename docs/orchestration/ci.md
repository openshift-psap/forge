# CI Entrypoint

The CI entrypoint provides standardized CI operations for FORGE projects using Click CLI framework.

## Structure

Each project has a `ci.py` file with:
- Main click group for the project
- Commands for CI phases (prepare, test, pre_cleanup)
- Shared error handling via `@ci.safe_ci_entrypoint` decorator

## Standard Commands

### prepare
Sets up environment and dependencies for testing.

### test
Executes the main testing logic for the project.

### pre_cleanup
Cleans up resources and finalizes the test run.

## Example Usage

```bash
# Run individual phases
python projects/llm_d/orchestration/ci.py pre-cleanup
python projects/llm_d/orchestration/ci.py prepare
python projects/llm_d/orchestration/ci.py test
```

or from the CI launcher:

```bash
./bin/run_ci llm_d ci pre-cleanup
./bin/run_ci llm_d ci prepare
./bin/run_ci llm_d ci test
```


## Error Handling

All commands are wrapped with `@ci.safe_ci_entrypoint` which:
- Catches exceptions and writes them to FAILURE files
- Provides consistent exit codes
- Maintains function metadata (name, docstring)
