#!/usr/bin/env python3
"""Trim bulky text fields from GuideLLM benchmark JSON files, in place.

Removes ``request_args`` (full message history), ``output`` and
``reasoning_output`` from request entries while preserving per-request
metrics (token counts, latencies, TTFT/ITL, ...) so that per-turn analysis
remains possible from the trimmed file.

With ``--drop-request-details``, the per-request records are removed
entirely from each benchmark entry (saving space); aggregate benchmark
data and the non-per-request ``benchmarker`` data are kept.

Usage: trim_benchmark.py [--drop-request-details] FILE [FILE ...]
"""

import argparse
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


def strip_requests(benchmark):
    if isinstance(benchmark, dict):
        return {k: v for k, v in benchmark.items() if k != "requests"}
    return benchmark


def trim(obj, drop_request_details=False):
    if drop_request_details:
        if isinstance(obj, dict):
            return {
                k: [strip_requests(i) for i in v] if k == "benchmarks" else trim(v, True)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [trim(i, True) for i in obj]
        return obj

    if isinstance(obj, dict):
        return {k: trim_entry(v) if k == "requests" else trim(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [trim(i) for i in obj]
    return obj


def trim_file(file_path, drop_request_details=False):
    tmp_path = f"{file_path}.trimtmp"
    with open(file_path) as f:
        data = json.load(f)
    try:
        with open(tmp_path, "w") as f:
            json.dump(trim(data, drop_request_details), f, separators=(",", ":"))
        os.replace(tmp_path, file_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-request-details",
        action="store_true",
        help="remove the per-request records from each benchmark entry entirely",
    )
    parser.add_argument("files", nargs="+", help="benchmark JSON files to trim in place")
    args = parser.parse_args(argv[1:])

    for file_path in args.files:
        trim_file(file_path, drop_request_details=args.drop_request_details)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
