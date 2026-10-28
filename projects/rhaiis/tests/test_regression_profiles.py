import logging
import re
from pathlib import Path

import pandas as pd
import yaml

from projects.rhaiis.postprocess.regression import METRICS, PROFILE_MAP, compare_runs

WORKLOADS_YAML = Path(__file__).parent.parent / "orchestration" / "config.d" / "workloads.yaml"

# profile5 rows are not collected for regression comparison, so its shape is
# intentionally absent from PROFILE_MAP.
UNCOMPARED_WORKLOADS = {"profile5"}


def _workload_shapes():
    """Parse the (prompt_tokens, output_tokens) shape of every workload in workloads.yaml."""
    workloads = yaml.safe_load(WORKLOADS_YAML.read_text())
    shapes = {}
    for key, cfg in workloads.items():
        isl = re.search(r"prompt_tokens=(\d+)", cfg["data"])
        osl = re.search(r"output_tokens=(\d+)", cfg["data"])
        if isl and osl:
            shapes[key] = (int(isl.group(1)), int(osl.group(1)))
    return shapes


def test_every_compared_workload_shape_is_in_profile_map():
    """Every compared workload must map to a profile, or its rows are silently excluded."""
    shapes = _workload_shapes()
    assert shapes, "no workload shapes parsed from workloads.yaml"
    missing = {
        key: shape
        for key, shape in shapes.items()
        if key not in UNCOMPARED_WORKLOADS and shape not in PROFILE_MAP
    }
    assert not missing, f"workload shapes missing from PROFILE_MAP: {missing}"


def _row(isl, osl, version, conc=25.0, output_tput=100.0):
    """Build one consolidated-CSV row for the comparator tests."""
    return {
        "model": "test/model",
        "accelerator": "H200",
        "TP": 1,
        "version": version,
        "prompt toks": isl,
        "output toks": osl,
        "intended concurrency": conc,
        "total_tok/sec": output_tput * 10,
        "output_tok/sec": output_tput,
        "ttft_p95": 1.0,
        "itl_p95": 1.0,
        "request_latency_median": 1.0,
    }


def _write_csv(path, rows):
    """Write rows to a CSV and return its path."""
    pd.DataFrame(rows).to_csv(path, index=False)
    return str(path)


def test_compare_runs_includes_profile6_and_profile7(tmp_path, caplog):
    """profile6/profile7 rows are compared; unmapped shapes are excluded with a warning."""
    current_csv = _write_csv(
        tmp_path / "current.csv",
        [
            _row(128, 128, "current"),
            _row(8000, 800, "current"),
            _row(333, 333, "current"),
            _row("unknown", "unknown", "current"),
            _row(float("nan"), float("nan"), "current"),
        ],
    )
    consolidated_csv = _write_csv(
        tmp_path / "consolidated.csv",
        [
            _row(128, 128, "baseline"),
            _row(8000, 800, "baseline"),
            _row(333, 333, "baseline"),
        ],
    )

    with caplog.at_level(logging.WARNING, logger="projects.rhaiis.postprocess.regression"):
        results, skip_reason = compare_runs(
            current_csv,
            consolidated_csv,
            compare_version="baseline",
            current_version="current",
            profile_map=PROFILE_MAP,
            metrics=METRICS,
        )

    assert skip_reason == ""
    assert {r.profile for r in results} == {"profile6", "profile7"}
    assert any("(333, 333)" in rec.getMessage() for rec in caplog.records)


def test_compare_runs_respects_restrict_profiles(tmp_path):
    """restrict_profiles still filters which profiles are compared."""
    current_csv = _write_csv(
        tmp_path / "current.csv",
        [_row(128, 128, "current"), _row(8000, 800, "current")],
    )
    consolidated_csv = _write_csv(
        tmp_path / "consolidated.csv",
        [_row(128, 128, "baseline"), _row(8000, 800, "baseline")],
    )

    results, skip_reason = compare_runs(
        current_csv,
        consolidated_csv,
        compare_version="baseline",
        current_version="current",
        profile_map=PROFILE_MAP,
        metrics=METRICS,
        restrict_profiles=["profile7"],
    )

    assert skip_reason == ""
    assert {r.profile for r in results} == {"profile7"}
