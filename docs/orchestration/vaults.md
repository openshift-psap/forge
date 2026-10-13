# Vaults and Secrets Management

FORGE provides a secure vault system for managing sensitive
information across projects. This system ensures that secrets are
properly organized, documented, and validated.

## Overview

The vault system manages:
- API keys and tokens
- Cluster credentials
- Database passwords
- Service account keys
- Certificate files
- Configuration secrets

## Vault Architecture

### Vault Definitions

Vaults are defined in YAML files located in `$FORGE_HOME/vaults/*.yaml`. Each vault specifies:

- **env_key**: Environment variable pointing to the vault's secret directory
- **description**: Documentation of the vault's purpose
- **content**: List of files/secrets within the vault

Example vault definition (`/vaults/psap-forge-ci.yaml`):

```yaml
env_key: PSAP_FORGE_FOURNOS_CI_SECRET_PATH
description: |
  Vault to access FOURNOS Kube API in the PSAP-mgmt cluster

content:
  kubeconfig:
    description: |
      KUBECONFIG file to access the PSAP-mgmt cluster with limited privileges

  server_url:
    description: |
      URL of the Kube-API server (having it in a dedicate file allows
      the CI engine to censor it in any output).
```

### Project Vault Requirements

Projects should validate the vault they use:

```python
from projects.core.library import vault

vault.init(["vault-1", "vault-2"])
```

This validates that the vaults...
1. exist (the env key exists and points to an existing directory)
2. have the expected content (the files listed in the vault YAML file exist)
3. do not contain un-referenced files

Currently the content of the files isn't validated, and the content of
the vault is not versioned. This could be added in the future.

## Vault Content Management

### Content Definition Formats

**Standard format** (recommended):
```yaml
content:
  kubeconfig:
    file: kubeconfig  # optional, defaults to content name
    description: |
      KUBECONFIG file for cluster access

  api_token:
    file: api.token
    description: |
      Authentication token for API access
```

### File Path Resolution

Vault content paths are resolved as:
```
{env_var_value}/{filename}
```

For example, with `PSAP_FORGE_FOURNOS_CI_SECRET_PATH=/secrets/ci` and content named `kubeconfig`:
```
/secrets/ci/kubeconfig
```

## Vault Validation

The vault system enforces strict validation to ensure security and documentation quality.

### Validation Rules

1. **Vault description required**: Every vault must have a non-empty description
2. **Content descriptions required**: Every piece of content must be documented
3. **File existence**: All defined content files must exist on filesystem
4. **No extra files**: Vault directories cannot contain undefined files
5. **Environment variables**: Required env vars must be set and point to existing directories

### Validation Levels

**Strict mode** (default): Fails on any validation error
**Warning mode**: Logs warnings but continues execution

Validation strictness is controlled globally:

```python
from projects.core.library import vault

# Disable strict validation globally (before initialization)
vault.disable_strict_validation()

# All subsequent vault operations will use warning mode
vault.init(["psap-forge-ci"])  # Uses non-strict validation
```

**When to use non-strict mode:**
- Development environments where vaults may be incomplete
- CI environments where some secrets are not available
- Testing scenarios where vault validation should not block execution
- Gradual vault migration where some legacy files may exist

**Important:** Call `disable_strict_validation()` before any vault initialization or operations.

### Using the Validation System

```python
from projects.core.library import vault
from projects.core.library.vault import get_vault_manager

# Control validation strictness globally
vault.disable_strict_validation()  # Optional: disable strict mode

# Initialize vault manager
vault_manager = get_vault_manager()

# Validate a specific vault
is_valid = vault_manager.validate_vault("psap-forge-ci")

# Validate all project vaults
is_valid = vault_manager.validate_project_vaults("llm_d")

# Validate all defined vaults
is_valid = vault_manager.validate_all_vaults()
```

### Getting Vault Content Paths

```python
from projects.core.library.vault import get_vault_content_path

# Get full path to vault content
kubeconfig_path = get_vault_content_path("psap-forge-ci", "kubeconfig")
# Returns: Path('/secrets/ci/kubeconfig')

# Access via vault object
vault = get_vault_manager().get_vault("psap-forge-ci")
kubeconfig_path = vault.content["kubeconfig"].file_path
# Returns: Path('/secrets/ci/kubeconfig')
```

## Environment Setup

### Auto-Discovery with `FORGE_VAULT_DIRECTORY`

Instead of setting individual environment variables for each vault, you
can point `FORGE_VAULT_DIRECTORY` to a single directory containing all
your vault subdirectories:

```bash
export FORGE_VAULT_DIRECTORY="/path/to/creds"
```

The directory should contain subdirectories named after vault
definitions (matching the YAML filenames in `$FORGE_HOME/vaults/`):

```
/path/to/creds/
├── psap-forge-ci/
│   ├── kubeconfig
│   └── server_url
├── psap-forge-hf/
│   └── hf_token
├── psap-forge-mlflow-export/
│   └── mlflow-secret.yaml
└── ...
```

On `vault.init()`, the vault module scans this directory and
automatically sets the corresponding environment variables (e.g.,
`PSAP_FORGE_FOURNOS_CI_SECRET_PATH=/path/to/creds/psap-forge-ci`).
Subdirectories that don't match any vault definition are silently
skipped. Environment variables that are already set are not overridden.

This mechanism is only active outside of Fournos CI (where
`FOURNOS_SECRETS` handles vault setup instead).

### Individual Environment Variables

Alternatively, each vault can be configured with its own environment variable:

```bash
export PSAP_FORGE_FOURNOS_CI_SECRET_PATH="/path/to/ci/secrets"
export PSAP_FORGE_NOTIFICATIONS_SECRET_PATH="/path/to/notification/secrets"
```

These take precedence over `FORGE_VAULT_DIRECTORY` auto-discovery.

### Directory Structure

Vault directories should contain only the files defined in the vault specification:

```
/path/to/ci/secrets/
├── kubeconfig          # Defined in vault spec
├── server_url          # Defined in vault spec
└── api_token           # Defined in vault spec
```

Any extra files will cause validation to fail.

## Creating New Vaults

### 1. Create Vault Definition

Create `/vaults/my-new-vault.yaml`:

```yaml
env_key: MY_VAULT_SECRET_PATH
description: |
  Description of what this vault contains and its purpose

content:
  credential_file:
    file: credentials.json
    description: |
      Service account credentials for accessing external API

  api_key:
    description: |
      API key for service authentication
```

### 2. Set Environment Variable

If using `FORGE_VAULT_DIRECTORY`, create the vault subdirectory under it:

```bash
mkdir -p $FORGE_VAULT_DIRECTORY/my-new-vault
```

Otherwise, set the individual environment variable:

```bash
export MY_VAULT_SECRET_PATH="/path/to/my/secrets"
```

### 3. Create Secret Files

```bash
# Use $FORGE_VAULT_DIRECTORY/my-new-vault or $MY_VAULT_SECRET_PATH
echo "secret-key-value" > $FORGE_VAULT_DIRECTORY/my-new-vault/api_key
cp service-credentials.json $FORGE_VAULT_DIRECTORY/my-new-vault/credentials.json
```

### 4. Add to Project Requirements

In `projects/my-project/orchestration/vaults.yaml`:

```yaml
- name: my-new-vault
  description: "Custom vault for my project"
```

### 5. Validate

```python
from projects.core.library.vault import validate_project_vaults

# Validate the new vault (uses global strict validation setting)
validate_project_vaults("my-project")
```

### 6. Expose the env var to the `forge_launcher`

```bash
forge_launcher config --set-env MY_VAULT_SECRET_PATH "/path/to/my/secrets"

# or, if the environment variable is already set in the terminal

forge_launcher config --pass-env MY_VAULT_SECRET_PATH
```

## Integration with FORGE Projects

### Initialization

Initialize vault system with specific vaults:

```python
from projects.core.library import vault

# Initialize with all vaults
vault.init()

# Initialize with specific vaults only
vault.init(["psap-forge-ci", "forge-llm-d"])
```

### Runtime Usage

```python
from projects.core.library.vault import get_vault_manager

# Get vault manager
vault_manager = get_vault_manager()

# Validate project vaults before execution
if not vault_manager.validate_project_vaults("llm_d"):
    raise RuntimeError("Vault validation failed")

# Get paths to vault content
vault = vault_manager.get_vault("forge-llm-d")
hf_token_path = vault.content["hf_token"].file_path
pull_secret_path = vault.content["rhoai_pull_secret"].file_path
```

## Error Handling

Common validation errors and solutions:

**Missing environment variable:**
```
Vault 'my-vault' requires environment variable MY_VAULT_SECRET_PATH to be set
```
Solution: Export the required environment variable

**Missing description:**
```
Vault 'my-vault' content 'api_key' is missing description
```
Solution: Add description to vault definition

**Extra files:**
```
Vault 'my-vault' contains extra file 'backup.txt' at '/path/vault/backup.txt' not defined in specification
```
Solution: Either remove the file or add it to the vault definition

**Missing content file:**
```
Vault 'my-vault' missing content 'api_key' at: /path/vault/api_key
```
Solution: Create the missing secret file
