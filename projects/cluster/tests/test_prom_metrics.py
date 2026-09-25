from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from projects.cluster.library.prom.metrics import (
    build_index,
    load_definitions,
    resolve,
    resolve_files,
    select,
    write_capture_input,
)

CLUSTER_METRICS_DIR = Path(__file__).resolve().parent.parent / "metrics"
KSERVE_METRICS_DIR = Path(__file__).resolve().parent.parent.parent / "kserve" / "metrics"

YAML_FILES = sorted(CLUSTER_METRICS_DIR.glob("*.yaml")) + sorted(KSERVE_METRICS_DIR.glob("*.yaml"))


def _write_yaml(tmp_path, name, data):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


class TestLoadDefinitions:
    def test_load_all_bundled_files(self):
        defs = load_definitions(*YAML_FILES)
        assert len(defs) > 0
        keys = [d.key for d in defs]
        assert len(keys) == len(set(keys))

    def test_all_definitions_have_required_fields(self):
        for defn in load_definitions(*YAML_FILES):
            assert defn.category
            assert defn.description
            assert defn.unit
            assert defn.promql
            assert defn.on_error in ("ignore", "fail")

    def test_duplicate_key_across_files_raises(self, tmp_path):
        f1 = _write_yaml(
            tmp_path,
            "a.yaml",
            {
                "my_metric": {"category": "x", "description": "d", "unit": "u", "promql": "up"},
            },
        )
        f2 = _write_yaml(
            tmp_path,
            "b.yaml",
            {
                "my_metric": {"category": "y", "description": "d", "unit": "u", "promql": "up"},
            },
        )
        with pytest.raises(ValueError, match="duplicate metric key"):
            load_definitions(f1, f2)

    def test_unknown_field_raises(self, tmp_path):
        f = _write_yaml(
            tmp_path,
            "bad.yaml",
            {
                "m": {"category": "x", "description": "d", "unit": "u", "promql": "up", "bogus": 1},
            },
        )
        with pytest.raises(ValueError, match="unknown fields"):
            load_definitions(f)

    def test_defaults_applied(self, tmp_path):
        f = _write_yaml(
            tmp_path,
            "minimal.yaml",
            {
                "m": {"category": "x", "description": "d", "unit": "u", "promql": "up"},
            },
        )
        defs = load_definitions(f)
        assert defs[0].on_error == "ignore"
        assert defs[0].tags == ()
        assert defs[0].params == ()


class TestSelect:
    @pytest.fixture()
    def defs(self):
        return load_definitions(*YAML_FILES)

    def test_filter_by_category(self, defs):
        result = select(defs, categories=["cpu"])
        assert all(d.category == "cpu" for d in result)
        assert len(result) > 0
        assert len(result) < len(defs)

    def test_filter_by_tags(self, defs):
        result = select(defs, tags=["per-pod"])
        assert all("per-pod" in d.tags for d in result)
        assert len(result) > 0
        assert len(result) < len(defs)

    def test_exclude_tags(self, defs):
        result = select(defs, exclude_tags=["per-node"])
        assert all("per-node" not in d.tags for d in result)

    def test_filter_by_keys(self, defs):
        result = select(defs, keys=["avg_cpu_usage_percent", "avg_memory_working_set_bytes"])
        assert {d.key for d in result} == {"avg_cpu_usage_percent", "avg_memory_working_set_bytes"}

    def test_combined_filters(self, defs):
        cpu = select(defs, categories=["cpu"])
        result = select(defs, categories=["cpu"], tags=["per-pod"])
        assert len(result) > 0
        assert len(result) <= len(cpu)
        assert all(d.category == "cpu" and "per-pod" in d.tags for d in result)


class TestResolve:
    def test_substitute_params(self, tmp_path):
        f = _write_yaml(
            tmp_path,
            "t.yaml",
            {
                "test_metric": {
                    "category": "x",
                    "description": "d",
                    "unit": "u",
                    "params": {"ns": {"description": "namespace regex"}},
                    "promql": 'up{namespace=~"{ns}"}',
                },
            },
        )
        defs = load_definitions(f)
        queries = resolve(defs, {"ns": "foo|bar"})
        assert queries == {"test_metric": 'up{namespace=~"foo|bar"}'}

    def test_default_param_used(self, tmp_path):
        f = _write_yaml(
            tmp_path,
            "t.yaml",
            {
                "test_metric": {
                    "category": "x",
                    "description": "d",
                    "unit": "u",
                    "params": {"ns": {"description": "ns", "default": "default-ns"}},
                    "promql": 'up{namespace=~"{ns}"}',
                },
            },
        )
        defs = load_definitions(f)
        queries = resolve(defs, {})
        assert queries == {"test_metric": 'up{namespace=~"default-ns"}'}

    def test_missing_mandatory_param_skips(self, tmp_path, caplog):
        f = _write_yaml(
            tmp_path,
            "t.yaml",
            {
                "m1": {
                    "category": "x",
                    "description": "d",
                    "unit": "u",
                    "params": {"ns": {"description": "ns"}},
                    "promql": "up",
                },
                "m2": {
                    "category": "x",
                    "description": "d",
                    "unit": "u",
                    "params": {"ns": {"description": "ns"}, "foo": {"description": "f"}},
                    "promql": "up",
                },
            },
        )
        defs = load_definitions(f)
        queries = resolve(defs, {})
        assert queries == {}
        assert "m1.ns" in caplog.text
        assert "m2.foo" in caplog.text

    def test_no_params_metric(self, tmp_path):
        f = _write_yaml(
            tmp_path,
            "t.yaml",
            {
                "simple": {
                    "category": "x",
                    "description": "d",
                    "unit": "u",
                    "promql": "up",
                },
            },
        )
        defs = load_definitions(f)
        queries = resolve(defs, {})
        assert queries == {"simple": "up"}

    def test_resolve_real_cpu_metrics(self):
        cpu_file = CLUSTER_METRICS_DIR / "resource_cpu.yaml"
        defs = load_definitions(cpu_file)
        queries = resolve(
            defs,
            {"namespace": "test-ns", "pod_name": "my-pod"},
        )
        assert len(queries) == len(defs)
        for promql in queries.values():
            assert "{namespace}" not in promql
            assert "test-ns" in promql


class TestWriteCaptureInput:
    def test_writes_flat_yaml(self, tmp_path):
        queries = {"cpu_usage": "sum(rate(...))", "memory": "sum(...)"}
        path = write_capture_input(queries, tmp_path / "input.yaml")
        assert path.exists()

        with path.open("r") as f:
            loaded = yaml.safe_load(f)
        assert loaded == queries

    def test_creates_parent_dirs(self, tmp_path):
        path = write_capture_input({"m": "up"}, tmp_path / "a" / "b" / "input.yaml")
        assert path.exists()


class TestBuildIndex:
    @pytest.fixture()
    def defs_file(self, tmp_path):
        return _write_yaml(
            tmp_path / "defs",
            "t.yaml",
            {
                "ok_metric": {
                    "category": "cpu",
                    "description": "d",
                    "unit": "cores",
                    "tags": ["rate"],
                    "params": {"ns": {"description": "ns"}},
                    "promql": 'up{ns="{ns}"}',
                },
                "empty_metric": {
                    "category": "cpu",
                    "description": "d2",
                    "unit": "cores",
                    "promql": "up",
                },
                "error_metric": {
                    "category": "mem",
                    "description": "d3",
                    "unit": "bytes",
                    "promql": "up",
                },
                "missing_metric": {
                    "category": "mem",
                    "description": "d4",
                    "unit": "bytes",
                    "promql": "up",
                },
            },
        )

    def test_builds_index_with_results(self, tmp_path, defs_file):
        defs = load_definitions(defs_file)

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "ok_metric.json").write_text(
            json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [{"metric": {}, "values": [[1, "1"]]}],
                    },
                }
            )
        )
        (results_dir / "empty_metric.json").write_text(
            json.dumps({"status": "success", "data": {"resultType": "matrix", "result": []}})
        )
        (results_dir / "error_metric.json").write_text(
            json.dumps({"status": "error", "errorType": "bad_data", "error": "parse error"})
        )

        index_path = build_index(
            defs, {"ns": "test"}, results_dir, timestamp="2026-01-01T00:00:00Z"
        )
        assert index_path.exists()

        with index_path.open("r") as fh:
            index = yaml.safe_load(fh)

        assert index["timestamp"] == "2026-01-01T00:00:00Z"
        assert index["results"]["ok_metric"]["status"] == "ok"
        assert index["results"]["ok_metric"]["category"] == "cpu"
        assert index["results"]["ok_metric"]["tags"] == ["rate"]
        assert index["results"]["ok_metric"]["params"] == {"ns": "test"}
        assert index["results"]["empty_metric"]["status"] == "no_data"
        assert index["results"]["error_metric"]["status"] == "error"
        assert index["results"]["missing_metric"]["status"] == "no_data"


class TestResolveFiles:
    def test_resolve_by_name(self):
        dirs = [CLUSTER_METRICS_DIR, KSERVE_METRICS_DIR]
        result = resolve_files(["resource_cpu", "vllm_latency"], dirs)
        assert len(result) == 2
        assert result[0] == CLUSTER_METRICS_DIR / "resource_cpu.yaml"
        assert result[1] == KSERVE_METRICS_DIR / "vllm_latency.yaml"

    def test_resolve_absolute_path(self):
        absolute = str(CLUSTER_METRICS_DIR / "workload.yaml")
        result = resolve_files([absolute], [])
        assert len(result) == 1
        assert result[0] == Path(absolute)

    def test_resolve_relative_path_with_slash(self):
        relative = str(CLUSTER_METRICS_DIR / "workload.yaml")
        result = resolve_files([relative], [])
        assert len(result) == 1

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError, match="no_such_file"):
            resolve_files(["no_such_file"], [CLUSTER_METRICS_DIR])

    def test_first_match_wins(self, tmp_path):
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "test.yaml").write_text(
            yaml.safe_dump(
                {"m1": {"category": "x", "description": "d", "unit": "u", "promql": "up"}}
            )
        )
        (d2 / "test.yaml").write_text(
            yaml.safe_dump(
                {"m2": {"category": "y", "description": "d", "unit": "u", "promql": "up"}}
            )
        )
        result = resolve_files(["test"], [d1, d2])
        assert result == [d1 / "test.yaml"]
