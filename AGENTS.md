# FORGE CI Framework — Agent Instructions

## Project Overview

FORGE is a CI/CD testing framework that orchestrates Kubernetes workloads on OpenShift clusters.
It uses a Python DSL to define tasks, apply manifests, and collect artifacts for debugging.

Key paths:
- `projects/` — individual project test harnesses
- `projects/NAME/orchestration` — upper layer of the test harness (config, prepare, test, cleanup)
- `projects/NAME/toolbox` — lower layer of the test harness (toolbox of scripts altering the state of the cluster, using the DSL language)
- `projects/NAME/test` — unit testing of the test harness
- `projects/core/dsl/` — shared DSL (task execution, k8s utilities, shell helpers)
- `fournos/gitops/` — Tekton pipelines and GitOps base workflows
- `docs/` — project documentation

## Security: Secret Handling (CRITICAL)

**NEVER write secrets or sensitive data to artifact directories, logs, or files.**

### Rules

1. **Never persist secrets to `ARTIFACT_DIR`.**
   The `oc_apply(path, manifest)` helper writes the manifest YAML to disk before applying it.
   If the manifest contains sensitive data (passwords, tokens, pull-secrets, certificates, API keys),
   it MUST NOT be written to any path under `env.ARTIFACT_DIR`.

2. **Use `oc("apply", "-f", "-", input_text=..., handled_secretly=True)` for secrets.**
   Apply secrets via stdin or a dedicated helper that does not persist the payload.
   Never use the standard `oc_apply()` for Secret-kind manifests.

3. **Never log secret values.**
   Do not print, log, or include in error messages: passwords, tokens, bearer credentials,
   TLS keys, pull-secret `.dockerconfigjson`, or any `data:`/`stringData:` field from a Secret.

4. **Check the `kind:` field.**
   Before calling `oc_apply(artifact_path, manifest)`, verify that `manifest.get("kind") != "Secret"`.
   If it is a Secret, use a non-persisting apply path.

5. **Artifact directories are public.**
   Treat everything under `ARTIFACT_DIR` as if it will be published on the internet.
   Only write non-sensitive operational data (CRs, logs, pod descriptions, events).
   Pod/Deployment and other resources are safe to store, as they must not contain secrets. See point 9.
   Kubernetes Secret objects should never be stored in the artifacts. Kubernetes Secret descriptions can be safely stored.

7. **Use the vault module.**
    Use the project `projects.core.library.vault` module to access the secrets, stored in files. Consider all the content of these files as secret.

8. **Never pass secrets via environment variables**
    For transparency and post-mortem troubleshooting, the environment variables are saved to disk at multiple locations. Do not use environment variables to pass a secret between two components. There might be few exceptions to this rule (MLFlow, AWS), but they must be handled with care.

9. **Never pass secrets via Pod environment variables**
   The Pod definition will be stored in public artifacts. Do no use plain text Pod environment variables to pass secrets to the Pod. Instead, use Secret resources and Pod environment variables that reference this secret.

10. **Never create secrets from literal values in commands.**
   Do not pass secret material through command-line arguments or other command text, including
   `--from-literal`, because commands may be captured in logs or artifacts. Write the secret to a
   securely created temporary file outside `ARTIFACT_DIR` and use `--from-file`. Never log the
   secret contents or the command containing them.

### Safe Patterns

```python
# GOOD: Apply a Secret without persisting to disk
def apply_secret(manifest: dict) -> None:
    """Apply a Secret manifest via stdin — nothing written to disk."""
    import yaml
    from projects.core.dsl.utils.k8s import oc

    oc("apply", "-f", "-", input_text=yaml.safe_dump(manifest, sort_keys=False))


# GOOD: Guard before using oc_apply
def safe_oc_apply(artifact_path, manifest):
    if manifest.get("kind") == "Secret":
        raise ValueError(
            f"Refusing to write Secret '{manifest['metadata']['name']}' to artifacts. "
            "Use apply_secret() instead."
        )
    oc_apply(artifact_path, manifest)


# GOOD: Pass secret data by file path; the value must never appear in command text
run_command(
    "create", "secret", "generic", "example-secret",
    "--from-file=SECRET_VALUE=/secure/temp/path",
)
```

### Forbidden Patterns

```python
# BAD: Writing a pull-secret to artifacts
src_dir = env.ARTIFACT_DIR / "src"
oc_apply(src_dir / "image-pull-secret.yaml", secret_manifest)  # LEAKS CREDENTIALS

# BAD: Logging secret content
logger.info(f"Applying secret: {manifest}")  # LEAKS if manifest has sensitive data

# BAD: Including tokens in error messages
raise RuntimeError(f"Failed to auth with token {token}")  # LEAKS TOKEN

# BAD: Passing secret material as a command-line literal; command text may be logged
run_command("create", "secret", "generic", "example-secret", "--from-literal=SECRET_VALUE=" + secret)
```

## Error Handling: Never Swallow Exceptions

**Never catch-and-warn silently.** A `logger.warning(...)` inside a bare `except` gets buried in logs and failures go unnoticed for weeks.

### Rules

1. **Raise typed exceptions.** Use `ValueError`, `FileNotFoundError`, `RuntimeError` — not `logger.warning()` + `return ""`.
   A missing config, missing vault secret, or failed version extraction is a real error.

2. **Register visible notifications for operational failures.**
   Use `ci.add_notification_file(name, message)` so failures appear in GitHub notifications,
   not just in log streams nobody reads.

3. **Bootstrap edge cases belong in the project, not shared libraries.**
   If a new nightly test has no previous MLflow run yet, the *project orchestration* should
   handle that gracefully (e.g., catch the exception and treat it as "no previous version").
   Shared/library code should raise — callers decide policy.

### Forbidden

```python
# BAD: Exception swallowed, failure invisible
try:
    upload_traces(data)
except Exception:
    logger.warning("Upload failed; continuing", exc_info=True)

# BAD: Returning empty string instead of raising
if connection is None:
    return ""  # caller has no idea something broke
```

### Correct

```python
# GOOD: Raise so caller can decide
if connection is None:
    raise ValueError("MLflow connection failed — check vault config")

# GOOD: If truly optional, still make it visible
try:
    upload_traces(data)
except Exception:
    logger.exception("Trace upload failed")
    ci.add_notification_file("trace-upload-failed", "Profiler trace upload failed — check logs")
    raise
```

## Layer Isolation: Toolbox Independence

Toolbox scripts are the **lower layer** — standalone, reusable cluster-state commands.
Orchestration is the **upper layer** — CI phases, config, presets.

### Rules

1. **Toolbox must never import from orchestration.**
   No `from projects.NAME.orchestration.runtime_config import cfg` in toolbox code.
   All inputs come through the `run()` entrypoint parameters for strong isolation.

2. **Pass all values explicitly via `run()` parameters.**
   Declare typed parameters with defaults in the entrypoint. This makes the command
   standalone, testable, and reviewable via `_meta/metadata.yaml`.

3. **Manifests and templates live with their toolbox command.**
   Put them in `projects/NAME/toolbox/COMMAND_NAME/templates/` — keeps the command self-contained.

### Forbidden

```python
# BAD: Toolbox importing orchestration config
from projects.llamastack.orchestration.runtime_config import cfg

hpa_cfg = cfg.get_hpa_config()
min_replicas = hpa_cfg.get("min_replicas", 1)
```

### Correct

```python
# GOOD: All config passed through entrypoint
@entrypoint
def run(*, min_replicas: int = 1, max_replicas: int = 4, memory_target: int = 75):
    """
    Configure HPA for the deployment.

    Args:
        min_replicas: Minimum replica count
        max_replicas: Maximum replica count
        memory_target: Target memory utilization percentage
    """
    return execute_tasks(locals())
```

## Waiting for State: No `time.sleep()`

### Rules

1. **Never use `time.sleep(N)` to wait for cluster state changes.**
   Arbitrary sleeps are unreliable — too short in slow environments, wasted time in fast ones.

2. **Use `@retry` to poll for readiness.**

```python
# GOOD: Poll until resource is ready
@retry(attempts=60, delay=30, backoff=1.0)
@task
def wait_until_ready(args, ctx):
    status = oc_get_json("datasciencecluster", "default-dsc")["status"]["phase"]
    return status == "Ready"
```

3. **Reuse existing wait toolbox scripts.**
   Before writing a new wait loop, check `projects/rhoai/toolbox/`, `projects/rhaiis/toolbox/`,
   `projects/core/` — many wait patterns already exist.

## Config Types: No Unnecessary Coercion

`config.project.get_config()` returns typed values from YAML. Do not cast.

```python
# BAD: YAML already returns int
replicas = int(config.project.get_config("runtime.replicas"))


# BAD: Custom bool coercion for YAML values
def _as_bool(value):
    return value.strip().lower() not in ("false", "0", "no")


# GOOD: Trust YAML types
replicas = config.project.get_config("runtime.replicas")
```

If a config value has the wrong type, fix the YAML — not the code.

## Don't Duplicate Framework Behavior

Before implementing preset handling, vault init, phase registration, or PR arg parsing —
check what `config.init()`, `ci_base`, and core library already provide.

- Preset application: handled by `config.init()`
- Config overrides: handled by `config.project.apply_config_overrides()`
- `/test` directives: handled at framework level

Duplicating framework behavior leads to drift and subtle bugs.

## Environment Variable Hygiene

If you temporarily set an environment variable, restore it in a `finally` block.

```python
# GOOD: Cleanup in finally
old_value = os.environ.get("MLFLOW_WORKSPACE")
try:
    os.environ["MLFLOW_WORKSPACE"] = workspace
    # ... do work ...
finally:
    if old_value is None:
        os.environ.pop("MLFLOW_WORKSPACE", None)
    else:
        os.environ["MLFLOW_WORKSPACE"] = old_value
```

## Legacy support

Overall, we do not want legacy support. Do not implement legacy fallback, unless explicitly requested by the user.
