# Inference Playbooks validation

Select a recipe by its consecutive directory path beneath `models/`,
`<model>/recipes/<recipe-folder>`, and provide an explicit manifest path relative
to that recipe folder:

```yaml
inference_playbooks:
  recipe: kimi-k3/recipes/h200-x8-pp2-tp8-8k1k
  manifest_path: manifests/vllm-v0.29.0/leaderworkerset.yaml
  namespace: provisioned-test-namespace
```

The recipe resolves to
`models/kimi-k3/recipes/h200-x8-pp2-tp8-8k1k/recipe.yaml`. Its declared `recipe_id`
is used for labels and evidence; the supplied locator is not rewritten in config.
Opaque recipe IDs, shortened `<model>/<recipe-folder>` selectors,
repository-root paths, absolute paths, traversal and symlink aliases are not
accepted. There is no repository scan or implicit path fallback for a selected
launch.

The explicit path selects one declared platform. Forge checks **all** immediate
YAML/YML files in that platform's manifest directory, including all documents.
Only an absent object namespace may be filled from the selected target namespace;
explicit namespaces must match. Services, Secrets, unsupported objects and hidden
serving repairs are rejected. Namespace provisioning, storage, credentials and
network prerequisites remain external.

With no recipe selected, the existing catalog-only validation performs no launch.
Bundle artifact filenames and repository-relative provenance paths are unchanged.
