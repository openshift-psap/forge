"""Public helpers for OpenMetrics dumps from promtool."""

import re
from typing import BinaryIO

_EXTENDED_LABEL_CANDIDATE = re.compile(
    rb'(?P<separator>[{,])(?P<name>(?![A-Za-z_][A-Za-z0-9_]*=)[^"\\\s{},=]+)(?==)'
)
# Consume quoted strings so a candidate inside a label value is left as-is.
_QUOTED_STRING_OR_EXTENDED_LABEL = re.compile(
    rb'"(?:[^"\\\r\n]|\\[^\r\n])*"|' + _EXTENDED_LABEL_CANDIDATE.pattern
)


def _quote_extended_label(match: re.Match[bytes]) -> bytes:
    if name := match["name"]:
        return match["separator"] + b'"' + name + b'"'
    return match[0]


def copy_openmetrics_with_quoted_labels(source: BinaryIO, destination: BinaryIO) -> None:
    """Copy an OpenMetrics stream, quoting label names emitted verbatim by older promtool."""
    for line in source:
        if not line.startswith(b"#") and _EXTENDED_LABEL_CANDIDATE.search(line):
            line = _QUOTED_STRING_OR_EXTENDED_LABEL.sub(_quote_extended_label, line)
        destination.write(line)
