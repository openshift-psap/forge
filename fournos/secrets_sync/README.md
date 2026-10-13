# YAML Secrets Sync

Synchronize Kubernetes secrets with a local YAML file.

## Usage

```bash
cd fournos/secrets_sync

# Extract secrets from the cluster
uv run sync_yaml_secrets.py extract

# Show differences between local file and the cluster
uv run sync_yaml_secrets.py diff

# Push local file to the cluster
uv run sync_yaml_secrets.py sync

# Preview changes without applying
uv run sync_yaml_secrets.py sync --dry-run
```

All commands default to `secrets.yaml` (git-ignored). Use `-n` to target a different namespace (default: `psap-secrets`).

## YAML format

```yaml
my-creds:
  username: admin
  password: s3cret
another-entry:
  api-key: abc123
```

Top-level keys are entry names (matching FournosJob `secretRefs`). Nested keys are the secret data.

## Typical workflow

1. `uv run sync_yaml_secrets.py extract` — pull current secrets
2. Edit `secrets.yaml`
3. `uv run sync_yaml_secrets.py diff` — review changes
4. `uv run sync_yaml_secrets.py sync` — push to cluster
5. **Delete `secrets.yaml`** — it contains plain-text secrets
