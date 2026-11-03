"""In-memory OTLP/JSON boundary before the Collector file exporter.

Standard library only. No payload logging, filesystem writes, credentials, or
retry queue. Failed sanitization/forwarding returns an error to the Collector,
which propagates it to the producer. The sanitized receiver is loopback-only.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from projects.cluster.library.tracing.sanitizer import sanitize_export_request

MAX_REQUEST_BYTES = 16 * 1024 * 1024


class SanitizerHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Request paths and HTTP parsing errors can contain sensitive text.

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self.respond(200 if self.path == "/health" else 404)

    def do_POST(self):
        if self.path != "/v1/traces":
            self.respond(404)
            return
        if self.headers.get("Content-Encoding", "identity") != "identity":
            self.respond(415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                self.respond(413)
                return
            record = json.loads(self.rfile.read(length))
            if not isinstance(record, dict) or not isinstance(record.get("resourceSpans"), list):
                raise ValueError("invalid OTLP request")
            sanitized = json.dumps(sanitize_export_request(record)).encode()
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            self.respond(400)
            return
        try:
            request = Request(
                self.server.destination, sanitized, {"Content-Type": "application/json"}
            )
            with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
                result = json.loads(response.read(MAX_REQUEST_BYTES))
                if result.get("partialSuccess", {}).get("rejectedSpans", 0):
                    raise ValueError("partial acceptance")
        except (OSError, URLError, ValueError):
            self.respond(503)
            return
        self.respond(200)


def serve():
    server = HTTPServer(("127.0.0.1", 4319), SanitizerHandler)
    server.destination = "http://127.0.0.1:4320/v1/traces"
    server.serve_forever()


if __name__ == "__main__":
    serve()
