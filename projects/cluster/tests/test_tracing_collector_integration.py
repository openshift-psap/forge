"""Opt-in proof with the pinned Collector and sanitizer containers.

Pre-pull DEFAULT_COLLECTOR_IMAGE and SANITIZER_IMAGE, then run with
FORGE_TEST_COLLECTOR=1. Uses only a disposable local pod and volume.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

from projects.cluster.library.tracing.collection import SANITIZER_IMAGE, sanitizer_sources
from projects.cluster.library.tracing.models import DEFAULT_COLLECTOR_IMAGE
from projects.cluster.tests.test_tracing_capture_toolbox import SENTINEL, _record
from projects.cluster.toolbox.deploy_trace_collector.main import TEMPLATES_DIR

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("FORGE_TEST_COLLECTOR") != "1",
        reason="requires opt-in local Podman fixture",
    ),
]


def command(*args):
    result = subprocess.run(
        ["podman", *args], check=True, capture_output=True, text=True, timeout=60
    )
    return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()


def poll(action, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = action()
            if result:
                return result
        except (OSError, subprocess.CalledProcessError):
            pass
        if time.monotonic() >= deadline:
            raise AssertionError("local collector did not reach expected state")
        time.sleep(0.2)


@pytest.fixture()
def collector(tmp_path, request):
    name = "forge-tracing-test-" + uuid.uuid4().hex[:10]
    sanitizer = name + "-sanitizer"
    volume = name + "-spool"
    source = tmp_path / "source"
    library = source / "projects/cluster/library/tracing"
    library.mkdir(parents=True)
    for key, content in sanitizer_sources().items():
        (source / key if key == "sanitize_otlp.py" else library / key).write_text(content)
    try:
        command("volume", "create", volume)
        storage = (
            ["--tmpfs", "/var/lib/forge-traces/spool:rw,size=64k,mode=1777"]
            if getattr(request, "param", False)
            else ["-v", volume + ":/var/lib/forge-traces/spool"]
        )
        command(
            "run",
            "--pull=never",
            "-d",
            "--name",
            name,
            *storage,
            "-p",
            "127.0.0.1::4318",
            "-e",
            "FORGE_TEST_CONFIG=" + (TEMPLATES_DIR / "collector-config.yaml").read_text(),
            DEFAULT_COLLECTOR_IMAGE,
            "--config=env:FORGE_TEST_CONFIG",
        )
        command(
            "create",
            "--pull=never",
            "--name",
            sanitizer,
            "--network",
            "container:" + name,
            SANITIZER_IMAGE,
            "python",
            "-B",
            "/opt/forge/sanitize_otlp.py",
        )
        command("cp", str(source), sanitizer + ":/opt/forge")
        command("start", sanitizer)
        port = command("port", name, "4318/tcp").rsplit(":", 1)[1]
        yield name, sanitizer, "http://127.0.0.1:" + port + "/v1/traces"
    finally:
        for container in (sanitizer, name):
            subprocess.run(["podman", "rm", "-f", container], capture_output=True, timeout=30)
        subprocess.run(["podman", "volume", "rm", "-f", volume], capture_output=True, timeout=30)


def post(uri, record):
    request = urllib.request.Request(
        uri, json.dumps(record).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status == 200


def test_first_persisted_spool_is_sanitized_and_restart_appends(collector, tmp_path):
    name, sanitizer, uri = collector
    record = _record("a" * 32, "forge-test", start_ns=100 * 10**9)
    span = record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["links"] = [
        {
            "traceId": "c" * 32,
            "spanId": "1" * 16,
            "traceState": "vendor=" + SENTINEL,
            "attributes": [
                {
                    "key": "server.address",
                    "value": {
                        "arrayValue": {
                            "values": [{"stringValue": "Bearer " + SENTINEL}],
                        }
                    },
                }
            ],
        }
    ]
    span["status"] = {"code": 2, "message": SENTINEL}
    child_record = _record("a" * 32, "forge-child", start_ns=100 * 10**9 + 123)
    child = child_record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    child["spanId"] = "2" * 16
    child["parentSpanId"] = span["spanId"]
    record["resourceSpans"].extend(child_record["resourceSpans"])
    poll(lambda: post(uri, record))
    spool = tmp_path / "spool.jsonl"

    def read_spool():
        command("cp", name + ":/var/lib/forge-traces/spool/otlp.jsonl", str(spool))
        return spool.read_text()

    first = poll(read_spool)
    assert SENTINEL not in first
    parsed = json.loads(first.splitlines()[0])
    actual = parsed["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert actual["traceId"] == "a" * 32
    assert actual["spanId"] == span["spanId"]
    assert actual["startTimeUnixNano"] == span["startTimeUnixNano"]
    assert actual["links"][0]["traceId"] == "c" * 32
    assert actual["status"]["code"] == 2
    assert actual["events"]
    actual_child = parsed["resourceSpans"][1]["scopeSpans"][0]["spans"][0]
    assert actual_child["parentSpanId"] == actual["spanId"]
    assert actual_child["startTimeUnixNano"] == child["startTimeUnixNano"]
    for container in (name, sanitizer):
        assert SENTINEL not in command("logs", container)

    # An unavailable sanitizer must reject ingress, never bypass sanitization.
    command("stop", sanitizer)
    with pytest.raises(urllib.error.HTTPError):
        post(uri, record)
    assert read_spool() == first
    command("restart", name)
    command("start", sanitizer)
    # Podman may allocate a new published host port on container restart.
    port = command("port", name, "4318/tcp").rsplit(":", 1)[1]
    uri = "http://127.0.0.1:" + port + "/v1/traces"
    record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "b" * 32
    poll(lambda: post(uri, record))
    combined = poll(lambda: text if "b" * 32 in (text := read_spool()) else None)
    assert combined.startswith(first)
    assert SENTINEL not in combined


@pytest.mark.parametrize("collector", [True], indirect=True)
def test_full_spool_rejects_ingress(collector):
    _name, _sanitizer, uri = collector
    record = _record("d" * 32, "forge-test", start_ns=100 * 10**9)
    poll(lambda: post(uri, record))
    # A large reviewed scalar survives sanitization and exhausts the 64 KiB
    # local spool. This models ENOSPC without filling the host filesystem.
    attrs = record["resourceSpans"][0]["resource"]["attributes"]
    attrs[0]["value"]["stringValue"] = "service-" + "x" * (256 * 1024)
    with pytest.raises(urllib.error.HTTPError):
        post(uri, record)
