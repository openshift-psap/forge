# Matrix Benchmarking

Matrix benchmarking runs a benchmark across all combinations of multiple
configuration dimensions (e.g. payload sizes x sweep profiles x endpoints).

The matrix is data-driven: dimensions and their values are declared in YAML
config. No code changes are needed to add or remove dimensions.

## Core module

`projects.core.library.matrix` provides three exports:

- **`MatrixCombination`** -- dataclass holding the `overrides` (config path to
  value), `labels` (for test metadata), and `artifact_dirname` for the current
  point in the matrix.
- **`compute_matrix(matrix_config)`** -- computes the cross-product of all
  dimensions and returns a list of `MatrixCombination`.
- **`activate_combination(combo)`** -- context manager that temporarily sets
  the config overrides for the duration of the block, then restores the
  original values.

## Configuration

Matrix config lives inside a project's test config (e.g.
`config.d/test.yaml`) under the `matrices` key. Each benchmark type (e.g.
`guidellm`) gets its own sub-key:

```yaml
matrices:
  guidellm:
    enabled: false
    labels:
      test.payload: payload
      test.sweep: sweep
    dimensions:
      test.payload: [XS, default, L]
      test.sweep: [smoke, small]
```

### Fields

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | `false` (default) disables the matrix -- the benchmark runs once with the scalar `test.payload` / `test.sweep` values. `true` activates matrix expansion. |
| `dimensions` | dict | Maps config paths to lists of values. Each config path must already exist in the project config. Scalar values are coerced to single-element lists. |
| `labels` | dict | Maps config paths to short label names used in test metadata and artifact directory names. When a config path has no label entry, the raw config path is used as the label key. |

## How it works

1. The orchestration reads the matrix config and calls `compute_matrix()`.
2. `compute_matrix()` returns the cross-product of all dimensions. With the
   config above, that produces 6 combinations:
   `XS/smoke`, `XS/small`, `default/smoke`, `default/small`, `L/smoke`,
   `L/small`.
3. For each combination, `activate_combination()` temporarily overrides the
   config values (e.g. sets `test.payload` to `"XS"` and `test.sweep` to
   `"smoke"`).
4. The existing benchmark loop runs inside the activated combination,
   reading the overridden config values as usual.
5. Matrix labels are stored in test metadata alongside existing labels
   (e.g. `{"endpoint": "svc", "payload": "XS", "sweep": "smoke"}`).

## Artifact directory structure

Each matrix combination gets its own artifact directory named from the
dimension values (sanitized to remove `/`, `=`, `'`, `"` characters):

```
XS_smoke/
  000__svc/
  001__maas/
  002__maas_praxis/
XS_small/
  000__svc/
  ...
default_smoke/
  000__svc/
  ...
```

When the matrix is disabled (`enabled: false`), the directory structure is
unchanged from the non-matrix behavior:

```
benchmarking/
  000__svc/
  001__maas/
  002__maas_praxis/
```

## Usage in orchestration code

```python
from projects.core.library.matrix import activate_combination, compute_matrix

matrix_config = config.project.get_config("test.matrices.guidellm", default_value={})
combinations = compute_matrix(matrix_config)

for combo in combinations:
    with activate_combination(combo):
        with env.NextArtifactDir(combo.artifact_dirname or "benchmarking"):
            run_benchmarks(..., matrix_labels=combo.labels)
```

## Adding a new dimension

To add a new dimension (e.g. vary `workloads.duration`):

1. Add the config path and values to `dimensions`:
   ```yaml
   dimensions:
     test.payload: [XS, default]
     workloads.duration: [60, 180]
   ```

2. Optionally add a short label:
   ```yaml
   labels:
     test.payload: payload
     workloads.duration: duration
   ```

3. The cross-product now includes the new dimension. No code changes required.
