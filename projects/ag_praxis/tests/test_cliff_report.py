from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from projects.ag_praxis.postprocess.ag_praxis.cliff_report import (
    _compute_stats,
    generate_500_cliff_report,
)
from projects.ag_praxis.postprocess.ag_praxis.k8s_state import load_praxis_k8s_state
from projects.ag_praxis.postprocess.ag_praxis.plugin import find_prometheus_capture_dirs
from projects.caliper.engine.model import BaseTestNode
from projects.caliper.postprocess.prometheus.parser import parse_capture_group

BASE_TIME = datetime(2026, 11, 23, 18, 30, 0, tzinfo=UTC)


# ── fixtures ───────────────────────────────────────────────────────────────


def _write_state_summary(
    artifacts: Path,
    *,
    annotation: int = 256,
    effective: int = 256,
    consistent: bool = True,
    identity_metrics: bool = False,
):
    """Write the praxis_state_summary.json of one observed state."""
    artifacts.mkdir(parents=True, exist_ok=True)

    (artifacts / "praxis_state_summary.json").write_text(
        json.dumps(
            {
                "extproc_max_requests": {
                    "annotation": annotation,
                    "effective": {
                        "payload-processing-extproc": {
                            "threshold_entries": 1,
                            "limits": [{"max_requests": effective}],
                        }
                    },
                    "consistent": consistent,
                },
                "identity_metrics": {
                    "policies": {
                        "maas-gateway-auth": {
                            "identity_metrics": identity_metrics,
                            "filters": 1,
                        }
                    },
                    "authconfigs": {
                        "identity_metrics": identity_metrics,
                        "total_filters": 2,
                    },
                },
                "authorino": {"replicas": 2},
            }
        )
    )


def _write_capture(
    capture_parent: Path,
    group_name: str,
    metrics: dict[str, list[tuple[dict, list[float]]]],
):
    """Write a kube-burner Prometheus capture dir parseable by parse_capture_group.

    metrics: metric name -> list of (labels, values); timestamps are synthesized.
    """
    capture_dir = capture_parent / group_name
    prom_dir = capture_dir / "prometheus_metrics"
    profiles_dir = capture_dir / "src" / "metric_profiles"
    prom_dir.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)

    results = {}
    for metric_name, series in metrics.items():
        points = []
        for labels, values in series:
            for i, value in enumerate(values):
                points.append(
                    {
                        "labels": labels,
                        "timestamp": (BASE_TIME + timedelta(minutes=i)).isoformat(),
                        "value": value,
                    }
                )
        (prom_dir / f"{metric_name}.json").write_text(json.dumps(points))
        results[metric_name] = {"status": "ok", "description": metric_name, "unit": ""}

    (prom_dir / "metrics_metadata.yaml").write_text(
        yaml.safe_dump({"timestamp": BASE_TIME.isoformat(), "variables": {}, "results": results})
    )
    profiles_dir.joinpath("profile.yaml").write_text(
        yaml.safe_dump(
            [{"query": "q", "metricName": name, "unit": ""} for name in metrics],
        )
    )
    return capture_dir


def _make_node(test_dir: Path) -> BaseTestNode:
    return BaseTestNode(
        directory=test_dir,
        test_labels={"endpoint": "maas_praxis"},
        artifact_paths=sorted(test_dir.rglob("*")),
        test_path=Path("000__maas_praxis"),
    )


def _cliff_metrics(*, instant_fraction: float, rate_200: float, rate_500: float):
    """The gateway metric set of one 500-cliff scenario at a given instant fraction."""
    instant_500 = rate_500 * instant_fraction
    return {
        "istio_requests_by_code": [
            ({"response_code": "200"}, [rate_200] * 3),
            ({"response_code": "500"}, [rate_500] * 3),
        ],
        "istio_requests_by_code_destination": [
            (
                {"response_code": "200", "destination_workload": "sim-llm-maas-kserve"},
                [rate_200] * 3,
            ),
            ({"response_code": "500", "destination_workload": "unknown"}, [rate_500] * 3),
        ],
        "istio_500_duration_bucket_rate": [
            ({"le": "1.0"}, [instant_500] * 3),
            ({"le": "+Inf"}, [rate_500] * 3),
        ],
        "istio_request_duration_p50_by_code": [
            ({"response_code": "200"}, [5000.0] * 3),
            ({"response_code": "500"}, [0.5 if instant_fraction > 0.5 else 1900.0] * 3),
        ],
        "istio_request_duration_p95_by_code": [
            ({"response_code": "200"}, [8000.0] * 3),
            ({"response_code": "500"}, [2000.0] * 3),
        ],
        "praxis_extproc_stream_duration_avg": [({}, [5.0] * 3)],
        "praxis_extproc_streams_inflight": [({}, [256.0, 256.0, 250.0])],
        "cpu_usage_by_pod": [({"pod": "authorino-abc"}, [3.0] * 3)],
    }


# ── k8s state loading ──────────────────────────────────────────────────────


def test_load_state_from_summary(tmp_path):
    test_dir = tmp_path / "000__maas_praxis"
    artifacts = test_dir / "007__capture_maas_praxis" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "praxis_state_summary.json").write_text(
        json.dumps(
            {
                "extproc_max_requests": {
                    "annotation": 256,
                    "effective": {
                        "payload-processing-extproc": {
                            "threshold_entries": 1,
                            "limits": [{"max_requests": 256}],
                        }
                    },
                    "consistent": True,
                },
                "identity_metrics": {"policies": {}, "authconfigs": None},
                "authorino": {"replicas": 2},
            }
        )
    )

    state = load_praxis_k8s_state(_make_node(test_dir))

    assert state is not None
    assert state.effective_budget() == 256
    assert state.authorino == {"replicas": 2}


def test_load_state_raw_artifacts_without_summary_returns_none(tmp_path):
    """Raw captured files alone are not a state source — only the summary is."""
    test_dir = tmp_path / "000__maas_praxis"
    artifacts = test_dir / "007__capture_maas_praxis" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "maastenantconfig.yaml").write_text("apiVersion: maas.opendatahub.io/v1")

    assert load_praxis_k8s_state(_make_node(test_dir)) is None


def test_load_state_prefers_newest_capture(tmp_path):
    test_dir = tmp_path / "000__maas_praxis"
    for dirname, budget in (("003__enable_maas_praxis_x", 128), ("009__capture_maas_praxis", 256)):
        artifacts = test_dir / dirname / "000__capture_maas_praxis" / "artifacts"
        _write_state_summary(artifacts, annotation=budget, effective=budget)

    state = load_praxis_k8s_state(_make_node(test_dir))

    assert state is not None
    assert state.extproc_max_requests["annotation"] == 256


def test_load_state_detects_budget_mismatch(tmp_path):
    test_dir = tmp_path / "000__maas_praxis"
    artifacts = test_dir / "007__capture_maas_praxis" / "artifacts"
    # annotated 2048 is out of range: the controller falls back to 8
    _write_state_summary(artifacts, annotation=2048, effective=8, consistent=False)

    state = load_praxis_k8s_state(_make_node(test_dir))

    assert state is not None
    assert state.extproc_max_requests["consistent"] is False
    assert state.effective_budget() == 8


def test_load_state_absent_returns_none(tmp_path):
    test_dir = tmp_path / "000__maas_praxis"
    (test_dir / "005__benchmarking").mkdir(parents=True)
    (test_dir / "005__benchmarking" / "results.json").write_text("{}")

    assert load_praxis_k8s_state(_make_node(test_dir)) is None


# ── regime classification ──────────────────────────────────────────────────


def _stats_for(
    tmp_path: Path,
    instant_fraction: float,
    rate_200: float = 50.0,
    rate_500: float = 150.0,
    budget=256,
    metrics=None,
):
    capture_dir = _write_capture(
        tmp_path,
        "000__capture_prometheus_metrics_istio_gateway",
        metrics
        if metrics is not None
        else _cliff_metrics(
            instant_fraction=instant_fraction, rate_200=rate_200, rate_500=rate_500
        ),
    )
    group = parse_capture_group(capture_dir)
    from projects.ag_praxis.postprocess.ag_praxis.cliff_report import (
        _find_authorino_cpu,
        _find_metric,
    )
    from projects.ag_praxis.postprocess.ag_praxis.k8s_state import PraxisK8sState

    state = PraxisK8sState(
        extproc_max_requests={
            "annotation": budget,
            "effective": {
                "payload-processing-extproc": {
                    "threshold_entries": 1,
                    "limits": [{"max_requests": budget}],
                }
            },
            "consistent": True,
        },
        identity_metrics={
            "policies": {"maas-gateway-auth": {"identity_metrics": False, "filters": 1}}
        },
        authorino=None,
        capture_dir=Path("."),
    )

    return _compute_stats(
        _find_metric([group], "istio_requests_by_code"),
        _find_metric([group], "istio_requests_by_code_destination"),
        _find_metric([group], "istio_500_duration_bucket_rate"),
        _find_metric([group], "istio_request_duration_p50_by_code"),
        _find_metric([group], "istio_request_duration_p95_by_code"),
        _find_metric([group], "praxis_extproc_stream_duration_avg"),
        _find_authorino_cpu([group]),
        state,
    )


def test_stats_overflow_regime(tmp_path):
    stats = _stats_for(tmp_path, instant_fraction=0.9, rate_200=50.0, rate_500=150.0)

    assert stats["frac_500"] == 0.75
    assert stats["instant_frac"] == 0.9
    assert stats["unknown_share"] == 1.0
    assert stats["budget"] == 256
    assert stats["predicted_ceiling"] == 256 / 5.0
    assert stats["peak_200"] == 50.0

    from projects.ag_praxis.postprocess.ag_praxis.cliff_report import _classify

    label, explanation = _classify(stats)
    assert label == "ext_proc overflow"
    assert "51 req/s" in explanation  # 256 / 5.0


def test_stats_deadline_regime(tmp_path):
    stats = _stats_for(tmp_path, instant_fraction=0.1, rate_200=50.0, rate_500=150.0)

    from projects.ag_praxis.postprocess.ag_praxis.cliff_report import _classify

    label, _ = _classify(stats)
    assert label == "auth deadline"


def test_stats_regime_unknown_without_duration_buckets(tmp_path):
    """Without the 500 duration buckets the regime stays unknown — the peak
    p50 is a display metric and must not be read as a run-wide instant
    fraction (a peak p50 of 0.5 ms used to force "ext_proc overflow")."""
    metrics = _cliff_metrics(instant_fraction=0.9, rate_200=50.0, rate_500=150.0)
    metrics.pop("istio_500_duration_bucket_rate")
    stats = _stats_for(tmp_path, instant_fraction=0.9, metrics=metrics)

    from projects.ag_praxis.postprocess.ag_praxis.cliff_report import _classify

    label, _ = _classify(stats)
    assert label == "500 failures"


def test_stats_frac_500_counts_all_response_codes(tmp_path):
    """The 500 fraction is over total gateway traffic, including codes other
    than 200 and 500 (e.g. 503s shed by the model workload)."""
    metrics = _cliff_metrics(instant_fraction=0.9, rate_200=50.0, rate_500=150.0)
    metrics["istio_requests_by_code"].append(({"response_code": "503"}, [100.0] * 3))
    stats = _stats_for(tmp_path, instant_fraction=0.9, metrics=metrics)

    assert stats["frac_500"] == 0.5  # 150 / (50 + 150 + 100)


def test_stats_clean_regime(tmp_path):
    stats = _stats_for(tmp_path, instant_fraction=0.0, rate_200=200.0, rate_500=1.0)

    from projects.ag_praxis.postprocess.ag_praxis.cliff_report import _classify

    label, _ = _classify(stats)
    assert label == "clean"


def test_clean_run_without_any_500_series(tmp_path):
    """A capture with no 500 series at all (clean run) must not crash the stats.

    The sum-by-code query returns no series for a response code that never
    occurred, so mean_500 is None while mean_200 is set — the 500 fraction is
    0.0, and the report must still generate (regression: None / total).
    """
    metrics = {
        "istio_requests_by_code": [({"response_code": "200"}, [50.0] * 3)],
        "istio_requests_by_code_destination": [
            ({"response_code": "200", "destination_workload": "sim-llm-maas-kserve"}, [50.0] * 3),
        ],
        "istio_request_duration_p50_by_code": [({"response_code": "200"}, [5000.0] * 3)],
        "praxis_extproc_stream_duration_avg": [({}, [5.0] * 3)],
    }
    stats = _stats_for(tmp_path, instant_fraction=0.0, metrics=metrics)

    assert stats["frac_500"] == 0.0
    assert stats["peak_500"] is None
    assert stats["predicted_ceiling"] == 256 / 5.0

    from projects.ag_praxis.postprocess.ag_praxis.cliff_report import _classify

    label, _ = _classify(stats)
    assert label == "clean"

    capture_dir = tmp_path / "000__capture_prometheus_metrics_istio_gateway"
    report_path = generate_500_cliff_report(
        [parse_capture_group(capture_dir)], None, tmp_path / "reports"
    )
    assert report_path is not None and report_path.exists()
    assert "Verdict: clean" in report_path.read_text(encoding="utf-8")


# ── end-to-end report generation ───────────────────────────────────────────


def test_generate_report_overflow_scenario(tmp_path):
    test_dir = tmp_path / "000__maas_praxis"
    artifacts = test_dir / "007__capture_maas_praxis" / "artifacts"
    _write_state_summary(artifacts, annotation=256, effective=256, identity_metrics=False)

    _write_capture(
        test_dir / "008__prometheus_metrics",
        "000__capture_prometheus_metrics_istio_gateway",
        _cliff_metrics(instant_fraction=0.9, rate_200=50.0, rate_500=150.0),
    )

    node = _make_node(test_dir)
    state = load_praxis_k8s_state(node)
    group_results = [parse_capture_group(d) for d in find_prometheus_capture_dirs(node)]

    report_path = generate_500_cliff_report(group_results, state, tmp_path / "reports")

    assert report_path is not None and report_path.exists()
    html = report_path.read_text(encoding="utf-8")
    assert "Verdict: ext_proc overflow" in html
    assert "Observed platform state" in html
    assert "maas-gateway-auth" in html
    assert "off" in html
    assert "256" in html
    assert "Envoy-local" in html


def test_generate_report_without_k8s_state(tmp_path):
    test_dir = tmp_path / "000__maas_praxis"
    capture_dir = _write_capture(
        test_dir / "008__prometheus_metrics",
        "000__capture_prometheus_metrics_istio_gateway",
        _cliff_metrics(instant_fraction=0.5, rate_200=50.0, rate_500=150.0),
    )

    group_results = [parse_capture_group(capture_dir)]

    report_path = generate_500_cliff_report(group_results, None, tmp_path / "reports")

    assert report_path is not None and report_path.exists()
    html = report_path.read_text(encoding="utf-8")
    assert "Verdict:" in html
    assert "Observed platform state" not in html


def test_generate_report_returns_none_without_data(tmp_path):
    assert generate_500_cliff_report([], None, tmp_path / "reports") is None
