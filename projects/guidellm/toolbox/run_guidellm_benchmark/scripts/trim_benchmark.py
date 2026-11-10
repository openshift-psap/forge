#!/usr/bin/env python3
"""Trim bulky text fields from GuideLLM benchmark JSON files, in place.

Removes ``request_args`` (full message history), ``output`` and
``reasoning_output`` from request entries while preserving per-request
metrics (token counts, latencies, TTFT/ITL, ...) so that per-turn analysis
remains possible from the trimmed file.

Usage: trim_benchmark.py FILE [FILE ...]
"""

import json
import os
import sys

BULK_FIELDS = {"request_args", "output", "reasoning_output"}


def trim_entry(obj):
    if isinstance(obj, dict):
        return {k: trim_entry(v) for k, v in obj.items() if k not in BULK_FIELDS}
    if isinstance(obj, list):
        return [trim_entry(i) for i in obj]
    return obj


def trim(obj):
    if isinstance(obj, dict):
        return {k: trim_entry(v) if k == "requests" else trim(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [trim(i) for i in obj]
    return obj


def trim_file(file_path):
    tmp_path = f"{file_path}.trimtmp"
    with open(file_path) as f:
        data = json.load(f)
    try:
        with open(tmp_path, "w") as f:
            json.dump(trim(data), f, separators=(",", ":"))
        os.replace(tmp_path, file_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def main(argv):
    if len(argv) < 2:
        print(f"usage: {argv[0]} FILE [FILE ...]", file=sys.stderr)
        return 2

    for file_path in argv[1:]:
        trim_file(file_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
