# Caliper

Artifact post-processing: parse labeled test trees, visualize, KPIs, export to OpenSearch / S3 / MLflow.

**Specification**: [specs/009-artifact-post-processing/spec.md](../../specs/009-artifact-post-processing/spec.md)

## CLI

- `--artifacts-dir` (`--base-dir`): root directory of the **test artifact tree** (where `__caliper_test_metadata__.yaml` lives). Manifest YAML is discovered here unless `--postprocess-config` points elsewhere.
- `--plugin-module` (`--plugin`): dotted Python **import path** for the plugin module (`get_plugin()`), overriding `plugin_module` in the manifest when both are set.

```bash
caliper --artifacts-dir /path/to/artifacts parse
caliper --plugin-module my_package.caliper_plugin --artifacts-dir /path visualize \
  --output-dir ./out --report-group default
```

Install optional backends: `pip install -e '.[caliper]'`

## Commands

| Command | Purpose |
|---------|---------|
| `parse` | Traverse, parse, write parse cache |
| `visualize` | Plots + HTML from unified model |
| `kpi generate` / `import` / `export` / `analyze` | Canonical KPI pipeline |
| `artifacts export` | File upload to S3 / MLflow |
| `ai-eval-export` | AI evaluation JSON |

See [quickstart.md](../../specs/009-artifact-post-processing/quickstart.md) and [plan.md](../../specs/009-artifact-post-processing/plan.md).
