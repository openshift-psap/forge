"""Tests for the Caliper reports index generation."""

from projects.core.library.reports_index import generate_caliper_reports_index


def _make_output_dir(tmp_path):
    """Build an output directory mimicking a real postprocess layout."""
    output_dir = tmp_path / "postprocessing"
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True)
    return output_dir, reports_dir


def _write_report(reports_dir, filename, content="<html>report</html>"):
    """Write a report file on disk and return its relative path."""
    path = reports_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return filename


def _status(output_dir, output_files):
    """Build a postprocess status dict like CaliperPostProcessor._build_result."""
    return {
        "final_status": "success",
        "base_directory": str(output_dir.parent),
        "steps": [
            {
                "visualize": {
                    "status": "success",
                    "output_dir": str(output_dir / "reports"),
                    "output_files": output_files,
                }
            }
        ],
    }


def test_index_groups_reports_by_scope_then_source(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    status = _status(
        output_dir,
        [
            {
                "source": "guidellm",
                "scope": "global",
                "title": "Performance Analysis",
                "path": _write_report(reports_dir, "performance_analysis.html"),
            },
            {
                "source": "prometheus_metrics",
                "scope": "000__maas_praxis",
                "title": "Ingress Metrics",
                "path": _write_report(reports_dir, "ingress.html"),
            },
        ],
    )

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    assert index_path is not None
    content = index_path.read_text()
    assert "<h2>Global</h2>" in content
    assert "<h3>Guidellm</h3>" in content
    assert ">Performance Analysis</a>" in content
    assert "<h2>Test 000__maas_praxis</h2>" in content
    assert "<h3>Prometheus Metrics</h3>" in content
    assert ">Ingress Metrics</a>" in content


def test_global_scope_comes_first(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    status = _status(
        output_dir,
        [
            {
                "source": "prometheus_metrics",
                "scope": "001__svc",
                "title": "Report B",
                "path": _write_report(reports_dir, "b.html"),
            },
            {
                "source": "prometheus_metrics",
                "scope": "000__maas_praxis",
                "title": "Report A",
                "path": _write_report(reports_dir, "a.html"),
            },
            {
                "source": "guidellm",
                "scope": "global",
                "title": "Report C",
                "path": _write_report(reports_dir, "c.html"),
            },
        ],
    )

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    content = index_path.read_text()
    assert content.index("<h2>Global</h2>") < content.index("<h2>Test 000__maas_praxis</h2>")
    assert content.index("<h2>Test 000__maas_praxis</h2>") < content.index("<h2>Test 001__svc</h2>")


def test_reports_within_source_keep_status_order(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    status = _status(
        output_dir,
        [
            {
                "source": "prometheus_metrics",
                "scope": "global",
                "title": "Zulu Report",
                "path": _write_report(reports_dir, "zulu.html"),
            },
            {
                "source": "prometheus_metrics",
                "scope": "global",
                "title": "Alpha Report",
                "path": _write_report(reports_dir, "alpha.html"),
            },
        ],
    )

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    content = index_path.read_text()
    assert content.index(">Zulu Report</a>") < content.index(">Alpha Report</a>")


def test_source_display_replaces_underscores(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    status = _status(
        output_dir,
        [
            {
                "source": "resource_usage_vs_limits",
                "scope": "global",
                "title": "Usage Report",
                "path": _write_report(reports_dir, "usage.html"),
            }
        ],
    )

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    content = index_path.read_text()
    assert "<h3>Resource Usage Vs Limits</h3>" in content


def test_stale_report_paths_are_dropped(tmp_path):
    output_dir, _ = _make_output_dir(tmp_path)
    status = _status(
        output_dir,
        [{"source": "guidellm", "scope": "global", "title": "Gone", "path": "missing.html"}],
    )

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    assert index_path is None
    assert not (output_dir / "reports_index.html").exists()


def test_no_visualize_step_skips_index(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    # HTML files exist on disk but no visualize step is recorded
    _write_report(reports_dir, "orphan.html")
    status = {
        "final_status": "success",
        "base_directory": str(output_dir.parent),
        "steps": [
            {
                "analyse_kpis": {
                    "status": "success",
                    "html_file": str(output_dir / "kpis" / "kpis.html"),
                }
            }
        ],
    }

    assert generate_caliper_reports_index(status, output_dir, "reports_index.html") is None


def test_html_files_on_disk_are_not_listed(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    # Files on disk but absent from the visualize status must not appear
    orphan = _write_report(reports_dir, "orphan.html")
    status = _status(
        output_dir,
        [
            {
                "source": "guidellm",
                "scope": "global",
                "title": "Listed Report",
                "path": _write_report(reports_dir, "listed.html"),
            }
        ],
    )

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    content = index_path.read_text()
    assert ">Listed Report</a>" in content
    assert orphan not in content


def test_missing_metadata_falls_back_to_defaults(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    status = _status(
        output_dir,
        [{"path": _write_report(reports_dir, "untitled.html")}],
    )

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    content = index_path.read_text()
    assert "<h2>Global</h2>" in content
    assert "<h3>Unknown</h3>" in content
    assert ">untitled</a>" in content


def test_failed_final_status_shows_error(tmp_path):
    output_dir, reports_dir = _make_output_dir(tmp_path)
    status = _status(
        output_dir,
        [
            {
                "source": "guidellm",
                "scope": "global",
                "title": "Report",
                "path": _write_report(reports_dir, "report.html"),
            }
        ],
    )
    status["final_status"] = "failed"

    index_path = generate_caliper_reports_index(status, output_dir, "reports_index.html")

    assert index_path is not None
    content = index_path.read_text()
    assert "status error" in content


def test_no_reports_returns_none(tmp_path):
    output_dir = tmp_path / "empty"
    output_dir.mkdir()

    assert generate_caliper_reports_index({}, output_dir, "reports_index.html") is None
    assert not (output_dir / "reports_index.html").exists()
