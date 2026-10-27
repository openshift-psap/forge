"""Bounded-retry and recoverable-upload tests.

These exercise the plan's Retry row without touching a live server: transient
HTTP/transport failures are retried a bounded number of times, and a receipt
written after accepted ingestion lets a later attempt retry only run linkage.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.tracing import exporter
from projects.caliper.engine.tracing.bundle import (
    ARCHIVE_FILENAME,
    SUMMARY_FILENAME,
    discover_bundles,
    sha256_of_file,
)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://mlflow.test/v1/traces", code, "test", {}, None)


@pytest.fixture()
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exporter, "_sleep_backoff", lambda _seconds: None)


def _post(**kwargs):
    exporter._post_traces(
        tracking_uri="http://mlflow.test",
        experiment_id="1",
        workspace="",
        body=b"payload",
        token=None,
        username=None,
        password=None,
        insecure_tls=False,
        server_cert_path=None,
        **kwargs,
    )


def test_post_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch, no_backoff: None):
    responses = [_http_error(429), _http_error(503), None]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(_request, timeout=None, context=None):
        response = responses.pop(0)
        if response is not None:
            raise response
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _post()
    assert responses == []


def test_post_retries_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch, no_backoff: None):
    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(_request, timeout=None, context=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("client timed out")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _post()
    assert calls == 2


def test_post_gives_up_after_bounded_attempts(monkeypatch: pytest.MonkeyPatch, no_backoff: None):
    calls = 0

    def fake_urlopen(_request, timeout=None, context=None):
        nonlocal calls
        calls += 1
        raise _http_error(500)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(exporter.TraceExportError, match="HTTP 500"):
        _post()
    assert calls == exporter.DEFAULT_MAX_ATTEMPTS


def test_link_retries_transient_failure(monkeypatch: pytest.MonkeyPatch, no_backoff: None):
    calls = 0

    class FakeClient:
        def link_traces_to_run(self, *, trace_ids, run_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient linkage failure")

    class FakeMlflow:
        tracking = type("Tracking", (), {"MlflowClient": FakeClient})

        @staticmethod
        def set_tracking_uri(_uri):
            pass

    import contextlib
    import sys

    import projects.caliper.engine.file_export.mlflow_secrets as secrets

    monkeypatch.setattr(secrets, "mlflow_connection_env", lambda _c: contextlib.nullcontext())
    monkeypatch.setitem(sys.modules, "mlflow", FakeMlflow)
    exporter._link_traces(
        trace_ids=["tr-1"],
        run_id="run-1",
        tracking_uri="http://mlflow.test",
        connection={},
    )
    assert calls == 2


def _write_bundle(root: Path) -> exporter.TraceBundle:
    node = root / "test_node"
    bundle_dir = node / "traces"
    bundle_dir.mkdir(parents=True)
    (node / "__caliper_test_metadata__.yaml").write_text(
        yaml.safe_dump({"version": "1", "labels": {"test_id": "t1"}}), encoding="utf-8"
    )
    archive = bundle_dir / ARCHIVE_FILENAME
    record = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                "spanId": "0000000000000001",
                                "name": "root",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000001000000000",
                            }
                        ]
                    }
                ]
            }
        ]
    }
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    summary = {
        "schema_version": 1,
        "identity": {"schema_version": 1, "test_id": "t1"},
        "config": {"mode": "standard"},
        "time": {},
        "data": {
            "archive_relative_path": ARCHIVE_FILENAME,
            "checksum": {"algorithm": "sha256", "value": sha256_of_file(archive)},
            "byte_count": archive.stat().st_size,
        },
        "quality": {},
        "status": {"state": "collected", "reason": ""},
    }
    (bundle_dir / SUMMARY_FILENAME).write_text(json.dumps(summary), encoding="utf-8")
    return discover_bundles(root)[0]


def test_link_failure_after_ingestion_retries_only_linkage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
):
    bundle = _write_bundle(tmp_path)
    sha = sha256_of_file(bundle.archive_path)
    posts = 0
    links = 0

    def fake_post(**_kwargs):
        nonlocal posts
        posts += 1

    def fake_link(**_kwargs):
        nonlocal links
        links += 1
        if links == 1:
            raise exporter.TraceExportError("trace linkage failed after 4 attempts")

    monkeypatch.setattr(exporter, "_post_traces", fake_post)
    monkeypatch.setattr(exporter, "_link_traces", fake_link)

    kwargs = dict(
        bundle=bundle,
        run_id="run-1",
        tracking_uri="http://mlflow.test",
        experiment_id="1",
        workspace="",
        connection=None,
        archive_sha256=sha,
    )
    with pytest.raises(exporter.TraceExportError):
        exporter.ingest_bundle(**kwargs)

    receipt = exporter.read_receipt(bundle.bundle_dir / "mlflow-export.json")
    assert receipt is not None
    assert receipt.ingested_trace_ids
    assert receipt.linked_trace_ids == []

    outcome = exporter.ingest_bundle(
        **kwargs, existing_receipt=exporter.read_receipt(bundle.bundle_dir / "mlflow-export.json")
    )
    assert posts == 1, "retry must not re-ingest an already accepted archive"
    assert links == 2
    assert outcome.linked == 1


def test_changed_archive_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
):
    bundle = _write_bundle(tmp_path)
    monkeypatch.setattr(exporter, "_post_traces", lambda **_kwargs: None)
    monkeypatch.setattr(exporter, "_link_traces", lambda **_kwargs: None)
    first = exporter.ingest_bundle(
        bundle=bundle,
        run_id="run-1",
        tracking_uri="http://mlflow.test",
        experiment_id="1",
        workspace="",
        connection=None,
        archive_sha256=sha256_of_file(bundle.archive_path),
    )
    assert first.status == "complete"

    with gzip.open(bundle.archive_path, "at", encoding="utf-8") as handle:
        handle.write(json.dumps({"resourceSpans": []}) + "\n")

    with pytest.raises(exporter.TraceExportError, match="does not match"):
        exporter.ingest_bundle(
            bundle=bundle,
            run_id="run-1",
            tracking_uri="http://mlflow.test",
            experiment_id="1",
            workspace="",
            connection=None,
            archive_sha256=sha256_of_file(bundle.archive_path),
            existing_receipt=exporter.read_receipt(bundle.bundle_dir / "mlflow-export.json"),
        )
