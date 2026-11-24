"""Shared Kubernetes Job execution and results for the MLflow k6 toolboxes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from projects.core.dsl.utils import truncate_k8s_name
from projects.core.dsl.utils.k8s import oc, oc_apply


@dataclass(frozen=True)
class PhaseHooks:
    """Optional callbacks invoked when a paused k6 workload is ready."""

    warmup: Callable[[], object] | None = None
    measurement: Callable[[], object] | None = None


_NO_PHASE_HOOKS = PhaseHooks()
phase_hooks = ContextVar("mlflow_k6_phase_hooks", default=_NO_PHASE_HOOKS)

_CONTROL_ADDRESS = "127.0.0.1:6565"
_CONTROL_SCRIPT = """
import http from 'k6/http';
import { sleep } from 'k6';
import exec from 'k6/execution';

export const options = { vus: 1, iterations: 1, maxDuration: '4m' };
const address = 'http://' + __ENV.MLFLOW_K6_CONTROL_ADDRESS;
const statusUrl = address + '/v1/status';

export default function () {
    if (__ENV.MLFLOW_K6_CONTROL_ACTION === 'prepare') {
        const deadline = Date.now() + 120000;
        while (Date.now() < deadline) {
            const response = http.get(statusUrl, { timeout: '2s' });
            // ExecutionStatusPausedBeforeRun is reached after VU initialization.
            if (response.status === 200 && response.json('data.attributes.status') === 4) {
                if (__ENV.MLFLOW_K6_RUN_SETUP === 'true') {
                    const setup = http.post(address + '/v1/setup', null, { timeout: '110s' });
                    if (setup.status !== 200) exec.test.abort('Workload setup failed: HTTP ' + setup.status);
                }
                return;
            }
            sleep(1);
        }
        exec.test.abort('k6 did not reach the paused state');
    }

    const payload = { data: { type: 'status', id: 'default', attributes: { paused: false } } };
    const response = http.patch(statusUrl, JSON.stringify(payload), {
        headers: { 'Content-Type': 'application/json' },
        timeout: '10s',
    });
    if (response.status !== 200) exec.test.abort('Could not resume k6: HTTP ' + response.status);
}
"""


@contextmanager
def synchronize_phases(hooks: PhaseHooks) -> Iterator[None]:
    """Set hooks for this run without adding callbacks to toolbox CLI metadata."""
    token = phase_hooks.set(hooks)
    try:
        yield
    finally:
        phase_hooks.reset(token)


@dataclass
class K6Results:
    """Raw output from a completed k6 Job."""

    raw_log: str
    measurement_start_time: datetime | None = None
    measurement_end_time: datetime | None = None

    @classmethod
    def from_job_log(cls, raw_log: str) -> K6Results:
        """Read the timestamps reported by the k6 scenario and teardown."""
        start = re.search(r"MLFLOW_K6_SCENARIO_START_MS=(\d+)", raw_log)
        end = re.search(r"MLFLOW_K6_SCENARIO_END_MS=(\d+)", raw_log)
        if start is None or end is None or int(end[1]) <= int(start[1]):
            raise RuntimeError("k6 Job logs do not contain a valid measurement window")
        return cls(
            raw_log=raw_log,
            measurement_start_time=datetime.fromtimestamp(int(start[1]) / 1000, UTC),
            measurement_end_time=datetime.fromtimestamp(int(end[1]) / 1000, UTC),
        )


def run_job(
    artifact_dir: Path,
    manifest: dict,
    phase: str,
    *,
    on_ready: Callable[[], object] | None = None,
    run_setup: bool = False,
) -> str:
    """Apply a k6 Job and save its logs before toolbox resource cleanup."""
    metadata = manifest["metadata"]
    job = f"job/{metadata['name']}"
    namespace = metadata["namespace"]
    if on_ready is not None:
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        container["args"][:0] = [f"--address={_CONTROL_ADDRESS}", "--paused", "--no-setup"]
    oc_apply(artifact_dir / f"{phase}-job.yaml", manifest)
    log_path = artifact_dir / f"{phase}-k6.log"
    try:
        if on_ready is not None:
            oc("wait", "--for=jsonpath={.status.ready}=1", job, "-n", namespace, "--timeout=180s")
            _control_job(job, namespace, "prepare", run_setup=run_setup)
            on_ready()
            _control_job(job, namespace, "resume")
        oc(
            "wait",
            "--for=condition=complete",
            job,
            "-n",
            namespace,
            "--timeout=900s",
            timeout_seconds=960,
        )
    except Exception:
        oc("logs", job, "-n", namespace, check=False, log_stdout=False, stdout_dest=log_path)
        raise
    return oc("logs", job, "-n", namespace, log_stdout=False, stdout_dest=log_path).stdout


def _control_job(job: str, namespace: str, action: str, *, run_setup: bool = False) -> None:
    """Reach the workload's loopback API using the k6 binary already in its container."""
    oc(
        "exec",
        "-i",
        "-n",
        namespace,
        job,
        "--",
        "k6",
        "run",
        "--quiet",
        "--no-usage-report",
        "--address=127.0.0.1:0",
        "-e",
        f"MLFLOW_K6_CONTROL_ADDRESS={_CONTROL_ADDRESS}",
        "-e",
        f"MLFLOW_K6_CONTROL_ACTION={action}",
        "-e",
        f"MLFLOW_K6_RUN_SETUP={str(run_setup).lower()}",
        "-",
        input_text=_CONTROL_SCRIPT,
        log_stdout=False,
        timeout_seconds=250,
    )


def selector_argument(selector: str | dict[str, str] | None) -> str | None:
    """Serialize Python selectors in the JSON format accepted by the CLI."""
    return json.dumps(selector) if isinstance(selector, dict) else selector


def validate_tracking_uri(tracking_uri: str) -> None:
    """Require a base URL suitable for manifest and command artifacts."""
    uri = urlsplit(tracking_uri)
    if (
        uri.scheme not in {"http", "https"}
        or not uri.hostname
        or uri.username is not None
        or uri.query
        or uri.fragment
    ):
        raise ValueError("tracking_uri must be an HTTP(S) base URL without credentials or a query")


def with_suffix(name: str, suffix: str) -> str:
    """Append a Kubernetes name suffix within the 63-character limit."""
    return f"{truncate_k8s_name(name, max_length=63 - len(suffix))}{suffix}".rstrip("-")
