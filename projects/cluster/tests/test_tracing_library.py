"""Unit tests for the shared tracing library.

Covers the plan's Configuration and Rendering rows: mode/ratio validation,
rejection of malformed YAML types and unsupported engines, idempotent
instrumentation, and preservation of existing non-tracing args and env.
"""

from __future__ import annotations

import pytest

from projects.cluster.library.tracing.instrumentation import (
    EXPORTER_ENDPOINT_ENV,
    RESOURCE_ATTRIBUTES_ENV,
    SERVICE_NAME_ENV,
    TRACES_SAMPLER_ARG_ENV,
    apply_env_vars,
    apply_vllm_additional_args,
    entrypoint_has_cli_arg,
    routing_proxy_tracing_args,
    tracing_environment,
    vllm_detailed_supported,
    vllm_tracing_args,
)
from projects.cluster.library.tracing.models import (
    CaptureIdentity,
    TracingConfigError,
    TracingMode,
    parse_mode,
    parse_sample_ratio,
    settings_from_config,
)


class _Config:
    """Minimal stand-in for config.project.get_config."""

    def __init__(self, data: dict):
        self._data = data

    def get_config(self, path, default_value=..., warn=True, print=True):
        node = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                if default_value is ...:
                    raise KeyError(path)
                return default_value
            node = node[part]
        return node


def _identity() -> CaptureIdentity:
    return CaptureIdentity(
        job_id="job-1",
        capture_id="cap-1",
        project="llm_d",
        namespace="ns",
        deployment_id="llmd-abc",
    )


# --- Configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", TracingMode.OFF),
        ("standard", TracingMode.STANDARD),
        ("detailed", TracingMode.DETAILED),
    ],
)
def test_parse_mode_accepts_supported(raw, expected):
    assert parse_mode(raw) is expected


def test_parse_mode_rejects_unknown():
    with pytest.raises(TracingConfigError, match="unsupported tracing.mode"):
        parse_mode("verbose")


def test_parse_mode_rejects_non_string():
    with pytest.raises(TracingConfigError, match="must be a string"):
        parse_mode(1)


@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
def test_parse_sample_ratio_accepts_bounds(value):
    assert parse_sample_ratio(value) == float(value)


@pytest.mark.parametrize("value", [-0.1, 1.1, "0.5", True])
def test_parse_sample_ratio_rejects_bad_values(value):
    with pytest.raises(TracingConfigError):
        parse_sample_ratio(value)


def test_settings_off_is_valid_even_with_malformed_collector_settings():
    cfg = _Config({"tracing": {"mode": "off", "sample_ratio": "not-a-number"}})
    settings = settings_from_config(cfg.get_config)
    assert settings.mode is TracingMode.OFF
    assert not settings.enabled


def test_settings_from_config_full():
    cfg = _Config(
        {
            "tracing": {
                "mode": "detailed",
                "sample_ratio": 0.25,
                "collector": {"storage_size": "5Gi", "ready_timeout_seconds": 120},
            }
        }
    )
    settings = settings_from_config(cfg.get_config)
    assert settings.mode is TracingMode.DETAILED
    assert settings.sample_ratio == 0.25
    assert settings.storage_size == "5Gi"
    assert settings.ready_timeout_seconds == 120


def test_settings_reject_malformed_storage_size():
    cfg = _Config({"tracing": {"mode": "standard", "collector": {"storage_size": 10}}})
    with pytest.raises(TracingConfigError, match="non-empty string"):
        settings_from_config(cfg.get_config)


def test_settings_reject_non_int_timeout():
    cfg = _Config({"tracing": {"mode": "standard", "collector": {"ready_timeout_seconds": "60"}}})
    with pytest.raises(TracingConfigError, match="integer"):
        settings_from_config(cfg.get_config)


# --- Instrumentation -------------------------------------------------------


def test_tracing_environment_sets_service_identity():
    settings = settings_from_config(
        _Config({"tracing": {"mode": "standard", "sample_ratio": 0.5}}).get_config
    )
    env = tracing_environment(settings, _identity(), "vllm-decode", namespace="ns")
    assert env[SERVICE_NAME_ENV] == "llmd-abc-vllm-decode"
    assert env[EXPORTER_ENDPOINT_ENV].startswith("http://forge-otel-collector-cap-1.ns.svc")
    assert env[TRACES_SAMPLER_ARG_ENV] == "0.5"
    assert "forge.capture_id=cap-1" in env[RESOURCE_ATTRIBUTES_ENV]
    # No credentials anywhere in the tracing environment.
    assert all("password" not in k.lower() and "token" not in k.lower() for k in env)


def test_vllm_args_are_idempotent_and_preserve_existing():
    settings = settings_from_config(_Config({"tracing": {"mode": "standard"}}).get_config)
    args = ["--model=x", "--otlp-traces-endpoint=http://old:4317", "--port=8000"]
    once = vllm_tracing_args(args, settings, _identity(), "vllm", namespace="ns")
    twice = vllm_tracing_args(once, settings, _identity(), "vllm", namespace="ns")
    assert once == twice
    assert "--model=x" in once
    assert "--port=8000" in once
    assert sum(a.startswith("--otlp-traces-endpoint") for a in once) == 1


def test_vllm_detailed_mode_adds_detailed_flag():
    settings = settings_from_config(_Config({"tracing": {"mode": "detailed"}}).get_config)
    args = vllm_tracing_args([], settings, _identity(), "vllm", namespace="ns")
    assert "--collect-detailed-traces=all" in args


def test_vllm_standard_mode_omits_detailed_flag():
    settings = settings_from_config(_Config({"tracing": {"mode": "standard"}}).get_config)
    args = vllm_tracing_args([], settings, _identity(), "vllm", namespace="ns")
    assert "--collect-detailed-traces=all" not in args


def test_routing_proxy_tracing_arg_is_idempotent():
    args = routing_proxy_tracing_args(["--tracing=true", "--foo=1"])
    assert args.count("--tracing=true") == 1
    assert "--foo=1" in args


def test_apply_vllm_additional_args_preserves_unrelated_and_dedups():
    existing = "--model=x --max-model-len=4096 --otlp-traces-endpoint=http://old:4317"
    merged = apply_vllm_additional_args(existing, ["--otlp-traces-endpoint=http://new:4317"])
    assert "--model=x" in merged
    assert "--max-len" not in merged
    assert merged.count("--otlp-traces-endpoint") == 1
    assert "http://new:4317" in merged


def test_apply_env_vars_replaces_existing_keys():
    env = [{"name": "KEEP", "value": "1"}, {"name": SERVICE_NAME_ENV, "value": "old"}]
    result = apply_env_vars(env, {SERVICE_NAME_ENV: "new"})
    assert {item["name"]: item["value"] for item in result} == {
        "KEEP": "1",
        SERVICE_NAME_ENV: "new",
    }


def test_entrypoint_has_cli_arg_detects_both_forms():
    assert entrypoint_has_cli_arg(["--otlp-traces-endpoint=x"], "--otlp-traces-endpoint")
    assert not entrypoint_has_cli_arg(["--other=x"], "--otlp-traces-endpoint")


def test_vllm_detailed_unsupported_engine_is_rejected():
    with pytest.raises(TracingConfigError, match="detailed"):
        vllm_detailed_supported("trtllm", mode=TracingMode.DETAILED)


def test_vllm_detailed_supported_for_standard_mode():
    assert vllm_detailed_supported("trtllm", mode=TracingMode.STANDARD)


def test_collector_writable_probe_runs_before_collector():
    """Storage writability is proved by a write-capable init container.

    The spool-reader stays read-only for artifact copying, so it must never be
    the container used to prove the PVC is writable.
    """
    from projects.cluster.library.tracing.collection import build_collector_resources
    from projects.cluster.library.tracing.models import (
        CaptureIdentity,
        TracingMode,
        TracingSettings,
    )

    resources = build_collector_resources(
        identity=CaptureIdentity(
            job_id="job",
            capture_id="cap",
            project="llm_d",
            namespace="ns",
            deployment_id="dep",
        ),
        settings=TracingSettings(mode=TracingMode.STANDARD),
        collector_config_text="receivers: {}",
    )
    deployment = next(r for r in resources if r["kind"] == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    init = pod_spec["initContainers"][0]
    assert init["name"] == "spool-probe"
    assert "touch" in init["command"][-1]
    assert init["volumeMounts"][0].get("readOnly") is not True

    reader = next(c for c in pod_spec["containers"] if c["name"] == "spool-reader")
    assert reader["volumeMounts"][0]["readOnly"] is True


def test_collector_config_uses_valid_file_exporter_schema():
    """The pinned file exporter must accept the rendered config at startup."""
    import yaml

    from projects.cluster.library.tracing.collection import load_collector_config
    from projects.cluster.toolbox.deploy_trace_collector.main import TEMPLATES_DIR

    config = yaml.safe_load(load_collector_config(TEMPLATES_DIR))
    file_exporter = config["exporters"]["file"]
    assert file_exporter["format"] == "json"
    assert file_exporter["append"] is True
    assert "rotation" not in file_exporter


def test_shipped_project_config_defaults_to_off(tmp_path):
    """The checked-in config.d/tracing.yaml must parse as mode "off".

    YAML 1.1 reads a bare ``off`` as boolean false, which strict config
    validation correctly rejects, so the shipped files must quote it.
    """
    from pathlib import Path as _Path

    import yaml

    from projects.cluster.library.tracing.models import TracingMode, settings_from_config

    repo_root = _Path(__file__).resolve().parents[3]
    for project in ("llm_d", "rhaiis"):
        config_path = (
            repo_root / "projects" / project / "orchestration" / "config.d" / "tracing.yaml"
        )
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert data["mode"] == "off", f'{project} must quote mode: "off"'

        def get_config(path, default=None, _data=data):
            key = path.removeprefix("tracing.")
            return _data.get(key, default)

        settings = settings_from_config(get_config)
        assert settings.mode is TracingMode.OFF
        assert settings.enabled is False


# --- Isolation -------------------------------------------------------------


def test_two_captures_in_one_namespace_do_not_collide():
    """Two jobs/captures in one namespace must produce distinct, scoped names."""
    from projects.cluster.library.tracing.collection import (
        build_collector_resources,
        cleanup_selector,
        collector_service_name,
        pvc_name,
    )
    from projects.cluster.library.tracing.models import TracingMode, TracingSettings

    settings = TracingSettings(mode=TracingMode.STANDARD)
    captures = {}
    for capture_id in ("cap-a", "cap-b"):
        identity = CaptureIdentity(
            job_id=f"job-{capture_id}",
            capture_id=capture_id,
            project="llm_d",
            namespace="ns",
            deployment_id="dep",
        )
        resources = build_collector_resources(
            identity=identity,
            settings=settings,
            collector_config_text="receivers: {}",
        )
        names = {r["metadata"]["name"] for r in resources}
        captures[capture_id] = (names, cleanup_selector(identity))

    names_a, selector_a = captures["cap-a"]
    names_b, selector_b = captures["cap-b"]
    assert names_a.isdisjoint(names_b)
    assert selector_a != selector_b
    assert selector_a.endswith("cap-a")
    assert collector_service_name("cap-a") != collector_service_name("cap-b")
    assert pvc_name("cap-a") != pvc_name("cap-b")


def test_cleanup_selector_only_matches_own_capture():
    from projects.cluster.library.tracing.collection import (
        OWNERSHIP_LABEL_KEY,
        build_collector_resources,
        cleanup_selector,
    )
    from projects.cluster.library.tracing.models import TracingMode, TracingSettings

    identity = CaptureIdentity(
        job_id="job-1",
        capture_id="cap-a",
        project="llm_d",
        namespace="ns",
        deployment_id="dep",
    )
    resources = build_collector_resources(
        identity=identity,
        settings=TracingSettings(mode=TracingMode.STANDARD),
        collector_config_text="receivers: {}",
    )
    selector = cleanup_selector(identity)
    key, _, value = selector.partition("=")
    assert key == OWNERSHIP_LABEL_KEY
    for resource in resources:
        assert resource["metadata"]["labels"][OWNERSHIP_LABEL_KEY] == value
