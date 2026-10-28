from __future__ import annotations

from pathlib import Path

import yaml

from projects.rhaiis.orchestration import runtime_config


class _FakeProject:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get_config(self, key: str, *args, **kwargs):
        value = self._values
        for part in key.split("."):
            value = value[part]
        return value


def _install_config(monkeypatch, *, benchmark: dict, model: dict) -> None:
    monkeypatch.setattr(
        runtime_config.config,
        "project",
        _FakeProject({"benchmarks": {"guidellm": benchmark}, "models": {"test-model": model}}),
    )


def test_get_benchmark_config_uses_model_default_when_unset(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        benchmark={
            "timeout": 14400,
            "args": {"backend_type": "openai_http", "request_type": None},
        },
        model={
            "hf_model_id": "openai/gpt-oss-120b",
            "benchmarks": {"guidellm": {"args": {"request_type": "/v1/completions"}}},
        },
    )

    benchmark_cfg = runtime_config.get_benchmark_config("test-model")

    assert benchmark_cfg["args"]["request_type"] == "/v1/completions"
    assert benchmark_cfg["args"]["backend_type"] == "openai_http"
    assert benchmark_cfg["timeout"] == 14400


def test_get_benchmark_config_preserves_explicit_request_type_override(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        benchmark={
            "args": {"backend_type": "openai_http", "request_type": "/v1/chat/completions"},
        },
        model={
            "hf_model_id": "openai/gpt-oss-120b",
            "benchmarks": {"guidellm": {"args": {"request_type": "/v1/completions"}}},
        },
    )

    benchmark_cfg = runtime_config.get_benchmark_config("test-model")

    assert benchmark_cfg["args"]["request_type"] == "/v1/chat/completions"


def test_get_benchmark_config_fills_missing_keys_without_overwriting(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        benchmark={
            "timeout": 14400,
            "args": {"backend_type": "openai_http", "request_type": None},
        },
        model={
            "hf_model_id": "openai/gpt-oss-120b",
            "benchmarks": {
                "guidellm": {
                    "timeout": 60,
                    "args": {"request_type": "/v1/completions", "rate_type": "concurrent"},
                }
            },
        },
    )

    benchmark_cfg = runtime_config.get_benchmark_config("test-model")

    assert benchmark_cfg["timeout"] == 14400
    assert benchmark_cfg["args"]["request_type"] == "/v1/completions"
    assert benchmark_cfg["args"]["rate_type"] == "concurrent"
    assert benchmark_cfg["args"]["backend_type"] == "openai_http"


def test_get_benchmark_config_leaves_global_value_without_model_override(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        benchmark={"args": {"backend_type": "openai_http", "request_type": None}},
        model={"hf_model_id": "openai/gpt-oss-120b"},
    )

    assert runtime_config.get_benchmark_config("test-model")["args"]["request_type"] is None
    assert runtime_config.get_benchmark_config()["args"]["request_type"] is None


def test_text_completion_models_define_request_type() -> None:
    models_path = Path(__file__).resolve().parents[1] / "orchestration" / "config.d" / "models.yaml"
    models = yaml.safe_load(models_path.read_text())

    for model_key in ("gpt-oss-120b", "gpt-oss-20b", "google-translategemma-12b"):
        request_type = models[model_key]["benchmarks"]["guidellm"]["args"]["request_type"]
        assert request_type == "/v1/completions"
