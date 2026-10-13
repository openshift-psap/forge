# capture_prometheus

Extracts cluster Prometheus metrics for a specific time window and saves them as a compressed OpenMetrics formated file. The output can later be imported into a local Prometheus instance for offline querying of any metric.

## How it works

1. **Symlink trick** — Creates a temporary directory on the Prometheus PVC containing only symlinks to `wal/` and `chunks_head/`. This limits `promtool` to scanning the recent in-memory data (~1-2 GB) instead of the full TSDB (which can be 20+ GB of persistent blocks).
2. **Time-filtered dump** — Runs `promtool tsdb dump-openmetrics --min-time=X --max-time=Y` inside the Prometheus container against the temp directory. Only samples within the requested window are emitted.
3. **Compress and copy** — The output is gzipped on the pod, then copied to the specified output directory via `oc cp`.
4. **Cleanup** — Removes the temp directory and compressed file from the pod.



## Limitations

- **Max test duration: 2 hours.** The WAL + chunks_head holds approximately the last 2 hours of un-compacted data. Tests longer than 2 hours will fail with an error. Support for including persistent blocks that overlap with the test window can be added later.
- **Data granularity:** The dump includes all metrics scraped by cluster monitoring (typically every 30s). For a 20-minute test this produces ~24M data points / ~130MB compressed.



## Usage



### Standalone (CLI)

```bash
./bin/run_toolbox cluster capture_prometheus \
    "2026-07-26T10:00:00+00:00" \
    "2026-07-26T10:20:00+00:00" \
    /path/to/output
```



### From orchestration 

```python
from projects.cluster.toolbox.capture_prometheus import main as capture_prom

prometheus_dir = env.ARTIFACT_DIR / "prometheus"
prometheus_dir.mkdir(parents=True, exist_ok=True)

capture_prom.run(
    start_time=test_start_time,
    end_time=test_end_time,
    output_dir=str(prometheus_dir),
)
```



## Querying the data locally

To analyze the captured metrics offline:

1. **Decompress** the archive to get a plain OpenMetrics text file.
2. **Import** the file into a Prometheus TSDB using `promtool tsdb create-blocks-from openmetrics`.
3. **Run a Prometheus container** (Docker, Podman, or any OCI runtime) with:
   - The imported TSDB directory mounted at `/prometheus`
   - A minimal config file (an empty `global: {}` is sufficient since no scraping is needed)
   - Retention set high enough that the data won't be garbage-collected (e.g. `--storage.tsdb.retention.time=365d`)
4. **Open the Prometheus UI** and query any metric using the time range from `metadata.json`.

The output file (`metrics.openmetrics.gz`) follows the standard [OpenMetrics](https://openmetrics.io/) format and is compatible with any tool that can ingest it (Prometheus, Grafana via Prometheus data source, etc.).

## Parameters


| Parameter     | Required | Default                | Description                                      |
| ------------- | -------- | ---------------------- | ------------------------------------------------ |
| `start_time`  | yes      | —                      | Start of capture window (ISO 8601, UTC)          |
| `end_time`    | yes      | —                      | End of capture window (ISO 8601, UTC)            |
| `output_dir`  | yes      | —                      | Directory to write `metrics.openmetrics.gz` into |
| `--pod-name`  | no       | `prometheus-k8s-0`     | Prometheus pod name                              |
| `--namespace` | no       | `openshift-monitoring` | Namespace of Prometheus                          |
| `--container` | no       | `prometheus`           | Container name in the pod                        |


