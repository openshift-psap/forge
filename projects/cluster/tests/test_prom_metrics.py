from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from projects.cluster.library.prom.metrics import (
    build_metadata_index,
    interpolate_variables,
    load_profile_metadata,
    resolve_files,
    resolve_params,
)  # noqa: E501

CLUSTER_METRICS_DIR = Path(__file__).resolve().parent.parent / "metrics"
KSERVE_METRICS_DIR = Path(__file__).resolve().parent.parent.parent / "kserve" / "metrics"

YAML_FILES = sorted(CLUSTER_METRICS_DIR.glob("*.yaml")) + sorted(KSERVE_METRICS_DIR.glob("*.yaml"))


def _write_profile(tmp_path, name, entries):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return p


class TestLoadProfileMetadata:
    @pytest.mark.parametrize("yaml_file", YAML_FILES, ids=lambda p: p.name)
    def test_load_real_profile(self, yaml_file):
        metadata = load_profile_metadata(yaml_file)
        assert len(metadata) > 0
        for meta in metadata:
            assert meta.metric_name
            assert meta.description
            assert meta.unit

    def test_load_all_bundled_files_no_duplicates(self):
        metadata = load_profile_metadata(*YAML_FILES)
        assert len(metadata) > 0
        names = [m.metric_name for m in metadata]
        assert len(names) == len(set(names))

    def test_basic_profile(self, tmp_path):
        f = _write_profile(
            tmp_path,
            "test.yaml",
            [
                {"query": "count(up)", "metricName": "my_metric", "description": "d", "unit": "u"},
            ],
        )
        metadata = load_profile_metadata(f)
        assert len(metadata) == 1
        assert metadata[0].metric_name == "my_metric"
        assert metadata[0].description == "d"
        assert metadata[0].unit == "u"

    def test_duplicate_metric_name_raises(self, tmp_path):
        f1 = _write_profile(
            tmp_path,
            "a.yaml",
            [{"query": "up", "metricName": "my_metric", "description": "d", "unit": "u"}],
        )
        f2 = _write_profile(
            tmp_path,
            "b.yaml",
            [{"query": "up", "metricName": "my_metric", "description": "d", "unit": "u"}],
        )
        with pytest.raises(ValueError, match="duplicate metricName"):
            load_profile_metadata(f1, f2)

    def test_missing_metric_name_raises(self, tmp_path):
        f = _write_profile(
            tmp_path,
            "bad.yaml",
            [{"query": "up", "description": "d", "unit": "u"}],
        )
        with pytest.raises(ValueError, match="missing required field 'metricName'"):
            load_profile_metadata(f)

    def test_missing_query_raises(self, tmp_path):
        f = _write_profile(
            tmp_path,
            "bad.yaml",
            [{"metricName": "m", "description": "d", "unit": "u"}],
        )
        with pytest.raises(ValueError, match="missing required field 'query'"):
            load_profile_metadata(f)

    def test_not_a_list_raises(self, tmp_path):
        f = _write_profile(
            tmp_path,
            "bad.yaml",
            {"my_metric": {"query": "up"}},
        )
        with pytest.raises(ValueError, match="non-empty list"):
            load_profile_metadata(f)

    def test_extra_fields_ignored(self, tmp_path):
        f = _write_profile(
            tmp_path,
            "extra.yaml",
            [
                {
                    "query": "count(up)",
                    "metricName": "m",
                    "description": "d",
                    "unit": "u",
                    "instant": True,
                    "captureStart": False,
                    "custom_field": "ignored",
                },
            ],
        )
        metadata = load_profile_metadata(f)
        assert len(metadata) == 1
        assert metadata[0].metric_name == "m"

    def test_missing_description_defaults_empty(self, tmp_path):
        f = _write_profile(
            tmp_path,
            "no_desc.yaml",
            [{"query": "up", "metricName": "m", "unit": "u"}],
        )
        metadata = load_profile_metadata(f)
        assert metadata[0].description == ""

    @pytest.mark.parametrize("yaml_file", YAML_FILES, ids=lambda p: p.name)
    def test_profiles_have_go_template_syntax(self, yaml_file):
        with yaml_file.open("r", encoding="utf-8") as f:
            entries = yaml.safe_load(f)
        for entry in entries:
            query = entry["query"]
            assert "{" not in query or "{{" in query, (
                f"{yaml_file.name}: {entry['metricName']} uses old {{param}} syntax "
                f"instead of Go template {{{{.PARAM}}}}"
            )


class TestResolveParams:
    def test_basic_resolution(self):
        result = resolve_params(
            {"namespace": "test-ns", "pod_name": "my-pod"},
        )
        assert result == {"namespace": "test-ns", "pod_name": "my-pod"}

    def test_runtime_override(self):
        result = resolve_params(
            {"namespace": "default", "name": "set_at_runtime"},
            runtime_params={"name": "actual-name"},
        )
        assert result == {"namespace": "default", "name": "actual-name"}

    def test_set_at_runtime_raises(self):
        with pytest.raises(ValueError, match="not set at runtime"):
            resolve_params({"name": "set_at_runtime"})

    def test_no_inter_param_interpolation(self):
        result = resolve_params(
            {"base": "my-service", "pod_name": "{base}-.*"},
        )
        assert result == {"base": "my-service", "pod_name": "{base}-.*"}

    def test_runtime_override_no_interpolation(self):
        result = resolve_params(
            {"name": "set_at_runtime", "pod_name": "{name}-.*"},
            runtime_params={"name": "svc"},
        )
        assert result == {"name": "svc", "pod_name": "{name}-.*"}


class TestInterpolateVariables:
    def test_basic_interpolation(self):
        result = interpolate_variables(
            {"NAMESPACE": "{llmisvc_namespace}", "POD_NAME": "{llmisvc_name}-.*"},
            {"llmisvc_namespace": "test-ns", "llmisvc_name": "my-svc"},
        )
        assert result == {"NAMESPACE": "test-ns", "POD_NAME": "my-svc-.*"}

    def test_passthrough_when_no_placeholder(self):
        result = interpolate_variables(
            {"NAMESPACE": "hardcoded-ns"},
            {"llmisvc_namespace": "other"},
        )
        assert result == {"NAMESPACE": "hardcoded-ns"}

    def test_unknown_placeholder_left_as_is(self):
        result = interpolate_variables(
            {"NAMESPACE": "{missing_var}"},
            {"other": "value"},
        )
        assert result == {"NAMESPACE": "{missing_var}"}

    def test_empty_variables(self):
        result = interpolate_variables({}, {"a": "1"})
        assert result == {}

    def test_empty_params(self):
        result = interpolate_variables({"K": "{v}"}, {})
        assert result == {"K": "{v}"}

    def test_multiple_placeholders_in_one_value(self):
        result = interpolate_variables(
            {"FULL": "{ns}/{name}"},
            {"ns": "default", "name": "pod-1"},
        )
        assert result == {"FULL": "default/pod-1"}


class TestBuildMetadataIndex:
    def test_builds_index_with_kube_burner_results(self, tmp_path):
        metadata = [
            load_profile_metadata(
                _write_profile(
                    tmp_path / "profiles",
                    "test.yaml",
                    [
                        {
                            "query": "up",
                            "metricName": "ok_metric",
                            "description": "d1",
                            "unit": "cores",
                        },
                        {
                            "query": "up",
                            "metricName": "empty_metric",
                            "description": "d2",
                            "unit": "cores",
                        },
                        {
                            "query": "up",
                            "metricName": "error_metric",
                            "description": "d3",
                            "unit": "bytes",
                        },
                        {
                            "query": "up",
                            "metricName": "missing_metric",
                            "description": "d4",
                            "unit": "bytes",
                        },
                    ],
                )
            )
        ]
        all_metadata = [m for group in metadata for m in group]

        results_dir = tmp_path / "results"
        results_dir.mkdir()

        (results_dir / "ok_metric.json").write_text(
            json.dumps(
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "labels": {},
                        "value": 1.0,
                        "metricName": "ok_metric",
                    },
                ]
            )
        )
        (results_dir / "empty_metric.json").write_text(json.dumps([]))
        (results_dir / "error_metric.json").write_text("not json")

        test_variables = {"NAMESPACE": "test-ns", "POD_NAME": "my-pod-.*"}

        index_path = build_metadata_index(
            all_metadata,
            results_dir,
            variables=test_variables,
            timestamp="2026-01-01T00:00:00Z",
        )
        assert index_path.exists()

        with index_path.open("r") as fh:
            index = yaml.safe_load(fh)

        assert index["timestamp"] == "2026-01-01T00:00:00Z"
        assert index["variables"] == test_variables
        assert index["results"]["ok_metric"]["status"] == "ok"
        assert index["results"]["ok_metric"]["description"] == "d1"
        assert index["results"]["ok_metric"]["unit"] == "cores"
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
        _write_profile(
            d1, "test.yaml", [{"query": "up", "metricName": "m1", "description": "d", "unit": "u"}]
        )
        _write_profile(
            d2, "test.yaml", [{"query": "up", "metricName": "m2", "description": "d", "unit": "u"}]
        )
        result = resolve_files(["test"], [d1, d2])
        assert result == [d1 / "test.yaml"]
