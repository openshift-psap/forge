import json

from projects.caliper.engine.model import UnifiedResultRecord, UnifiedRunModel
from projects.guidellm.postprocess.guidellm import dashboard as guidellm_dashboard
from projects.rhaiis.orchestration import test_phase
from projects.rhaiis.postprocess import plugin
from projects.rhaiis.postprocess.csv_dashboard import RHAIIS_FIELDNAMES, serialize_env_vars


def test_serialize_env_vars_redacts_sensitive_values_and_sorts_keys():
    serialized = serialize_env_vars(
        {
            "VLLM_ROCM_USE_AITER_MOE": 0,
            "HUGGING_FACE_HUB_TOKEN": "must-not-be-written",
            "NUMBA_NUM_THREADS": 16,
        }
    )

    assert json.loads(serialized) == {
        "HUGGING_FACE_HUB_TOKEN": "<redacted>",
        "NUMBA_NUM_THREADS": "16",
        "VLLM_ROCM_USE_AITER_MOE": "0",
    }
    assert "must-not-be-written" not in serialized
    assert serialized == (
        '{"HUGGING_FACE_HUB_TOKEN":"<redacted>","NUMBA_NUM_THREADS":"16",'
        '"VLLM_ROCM_USE_AITER_MOE":"0"}'
    )


def test_serialize_empty_env_vars_as_empty_string():
    assert serialize_env_vars(None) == ""
    assert serialize_env_vars({}) == ""


def test_create_test_labels_include_serialized_env_vars_without_logging_values(monkeypatch, caplog):
    captured = {}
    monkeypatch.setattr(
        test_phase,
        "create_test_metadata",
        lambda _directory, labels: captured.update(labels),
    )

    test_phase._create_test_labels(
        "model-key",
        "profile1",
        "nvidia",
        {},
        hf_model_id="org/model",
        env_vars={"VLLM_USE_V1": 1, "HUGGING_FACE_HUB_TOKEN": "must-not-be-logged"},
    )

    assert captured["env_vars"] == ('{"HUGGING_FACE_HUB_TOKEN":"<redacted>","VLLM_USE_V1":"1"}')
    assert "must-not-be-logged" not in caplog.text
    assert '"env_vars"' not in caplog.text


def test_dashboard_export_maps_env_vars_to_csv_column(monkeypatch, tmp_path):
    captured = {}

    class StubDashboardCsvExporter:
        def export_dashboard_csv(
            self,
            _model,
            output_path,
            *,
            prefix,
            fieldnames,
            metadata_row_mapper,
        ):
            captured["prefix"] = prefix
            captured["fieldnames"] = fieldnames
            captured["row"] = metadata_row_mapper({"env_vars": '{"VLLM_USE_V1":"1"}'})
            return str(output_path)

    monkeypatch.setattr(
        guidellm_dashboard,
        "DashboardCsvExporter",
        StubDashboardCsvExporter,
    )

    output_path = tmp_path / "dashboard.csv"
    result = plugin.RhaiisPlugin().export_dashboard_csv(object(), output_path)

    assert result == str(output_path)
    assert captured["prefix"] == "rhaiis"
    assert "env_vars" in RHAIIS_FIELDNAMES
    assert captured["fieldnames"] is RHAIIS_FIELDNAMES
    assert captured["row"]["env_vars"] == '{"VLLM_USE_V1":"1"}'


def test_dashboard_kpis_preserve_env_vars_from_test_labels():
    model = UnifiedRunModel(
        plugin_module="rhaiis",
        base_directory="/tmp/artifacts",
        test_nodes=[],
        unified_result_records=[
            UnifiedResultRecord(
                test_base_path="profile1",
                distinguishing_labels={"env_vars": '{"VLLM_USE_V1":"1"}'},
                metrics={
                    "request_rate": [1],
                    "performance_curves": {
                        "output_tok_per_sec": [100],
                        "intended_concurrency": [1],
                    },
                },
                run_identity={"guidellm": True},
            )
        ],
    )

    kpis = guidellm_dashboard.compute_dashboard_kpis(model, prefix="rhaiis")

    assert kpis
    assert kpis[0].labels["env_vars"] == '{"VLLM_USE_V1":"1"}'
