"""The pre-persistence HTTP boundary rejects invalid input and downstream failure."""

import io
import json
import runpy
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from projects.cluster.tests.test_tracing_capture_toolbox import SENTINEL, _record
from projects.cluster.toolbox.deploy_trace_collector.main import TEMPLATES_DIR


@pytest.mark.parametrize(
    "case,expected",
    [
        ("valid", 200),
        ("invalid", 400),
        ("oversized", 413),
        ("downstream", 503),
        ("partial", 503),
        ("compressed", 415),
    ],
)
def test_sanitizer_boundary(monkeypatch, case, expected):
    module = runpy.run_path(str(TEMPLATES_DIR / "sanitize_otlp.py"))
    handler = object.__new__(module["SanitizerHandler"])
    raw = json.dumps(_record("a" * 32, "service", start_ns=100 * 10**9)).encode()
    if case == "invalid":
        raw = b"not-json"
    handler.headers = {"Content-Length": str(len(raw))}
    if case == "oversized":
        handler.headers["Content-Length"] = str(module["MAX_REQUEST_BYTES"] + 1)
    if case == "compressed":
        handler.headers["Content-Encoding"] = "gzip"
    handler.rfile = io.BytesIO(raw)
    handler.path = "/v1/traces"
    handler.server = SimpleNamespace(destination="http://127.0.0.1:4320/v1/traces")
    handler.respond = Mock()
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = (
        b'{"partialSuccess":{"rejectedSpans":1}}' if case == "partial" else b"{}"
    )
    opener = Mock()
    opener.open.return_value = response
    if case == "downstream":
        opener.open.side_effect = URLError("synthetic downstream failure")
    monkeypatch.setitem(handler.do_POST.__func__.__globals__, "build_opener", lambda *_: opener)
    handler.do_POST()
    handler.respond.assert_called_once_with(expected)
    if case in ("invalid", "oversized", "compressed"):
        opener.open.assert_not_called()
    else:
        outgoing = opener.open.call_args.args[0].data
        assert SENTINEL.encode() not in outgoing
        assert b"a" * 32 in outgoing
