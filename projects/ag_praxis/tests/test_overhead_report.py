from __future__ import annotations

from pathlib import Path

import yaml

from projects.ag_praxis.postprocess.ag_praxis.plugin import _generate_overhead_reports
from projects.caliper.engine.model import UnifiedResultRecord, UnifiedRunModel

CONFIG_YAML = {
    "platform": {
        "sim": {
            "time_to_first_token": "100ms",
            "inter_token_latency": "10ms",
        }
    },
    "test": {"payload": "default"},
    "workloads": {
        "payloads": {
            "default": {"prompt_tokens": 100, "output_tokens": 50},
        }
    },
}


def _write_config(base_dir: Path, test_base_path: str):
    config_path = base_dir / test_base_path / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(CONFIG_YAML))


def _endpoint_record(test_base_path: str, endpoint: str, latency_s: float = 1.0):
    return UnifiedResultRecord(
        test_base_path=test_base_path,
        distinguishing_labels={"endpoint": endpoint},
        metrics={
            "performance_curves": {
                "intended_concurrency": [1.0],
                "request_latency_median": [latency_s],
            }
        },
        run_identity={"guidellm": True},
    )


def _make_model(base_dir: Path, records: list[UnifiedResultRecord]) -> UnifiedRunModel:
    return UnifiedRunModel(
        plugin_module="projects.ag_praxis.postprocess.ag_praxis.plugin",
        base_directory=str(base_dir),
        test_nodes=[],
        unified_result_records=records,
    )


def _make_group(base_dir: Path, group_dir: str, endpoints: list[str]):
    """Create endpoint records (and their config.yaml) for one matrix combination."""
    records = []
    for idx, endpoint in enumerate(endpoints):
        test_base_path = str(Path(group_dir) / f"{idx:03d}__{endpoint.replace('/', '_')}")
        _write_config(base_dir, test_base_path)
        records.append(_endpoint_record(test_base_path, endpoint))
    return records


def test_one_report_per_matrix_combination(tmp_path):
    records = [
        *_make_group(tmp_path, "test/000__simA", ["svc", "maas/ipp", "maas/praxis"]),
        *_make_group(tmp_path, "test/001__simB", ["svc", "maas/ipp", "maas/praxis"]),
    ]
    model = _make_model(tmp_path, records)
    output_dir = tmp_path / "postprocessing"

    reports = _generate_overhead_reports(model, output_dir)

    assert [r.scope for r in reports] == ["test/000__simA", "test/001__simB"]
    for report in reports:
        assert report.title == "Overhead Analysis"
        report_file = output_dir / report.path
        assert report_file.is_file()
        html = report_file.read_text(encoding="utf-8")
        # each report compares the endpoints of its own combination only
        assert "maas/praxis" in html
        assert "Concurrency: 1 streams" in html


def test_single_test_without_matrix(tmp_path):
    records = _make_group(tmp_path, "test", ["svc", "maas/ipp", "maas/praxis"])
    model = _make_model(tmp_path, records)
    output_dir = tmp_path / "postprocessing"

    reports = _generate_overhead_reports(model, output_dir)

    assert len(reports) == 1
    assert reports[0].scope == "test"
    assert (output_dir / reports[0].path).is_file()


def test_groups_do_not_mix_endpoints(tmp_path):
    """A combination missing endpoints must not borrow them from another combination."""
    records = [
        *_make_group(tmp_path, "test/000__full", ["svc", "maas/ipp", "maas/praxis"]),
        *_make_group(tmp_path, "test/001__svc_only", ["svc"]),
    ]
    model = _make_model(tmp_path, records)
    output_dir = tmp_path / "postprocessing"

    reports = _generate_overhead_reports(model, output_dir)

    assert [r.scope for r in reports] == ["test/000__full", "test/001__svc_only"]

    svc_only_html = (output_dir / reports[1].path).read_text(encoding="utf-8")
    assert "Missing endpoints: maas/ipp, maas/praxis" in svc_only_html

    full_html = (output_dir / reports[0].path).read_text(encoding="utf-8")
    assert "Missing endpoints" not in full_html


def test_no_report_without_endpoint_records(tmp_path):
    record = UnifiedResultRecord(
        test_base_path="test/000__svc",
        distinguishing_labels={"facet": "default"},
        metrics={},
        run_identity={"guidellm": True},
    )
    model = _make_model(tmp_path, [record])
    output_dir = tmp_path / "postprocessing"

    reports = _generate_overhead_reports(model, output_dir)

    assert reports == []


def test_skips_records_without_benchmarks(tmp_path):
    empty_record = UnifiedResultRecord(
        test_base_path="test/000__svc",
        distinguishing_labels={"endpoint": "svc"},
        metrics={"no_benchmarks_found": True},
        run_identity={"guidellm": True},
    )
    model = _make_model(tmp_path, [empty_record])
    output_dir = tmp_path / "postprocessing"

    reports = _generate_overhead_reports(model, output_dir)

    assert reports == []
