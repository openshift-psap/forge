from __future__ import annotations

import pytest
import yaml

from projects.core.ci_entrypoint.github import pr_args


def test_handle_var_directive_preserves_yaml_scalar_types() -> None:
    assert pr_args.handle_var_directive("/var feature.enabled: false") == {"feature.enabled": False}
    assert pr_args.handle_var_directive("/var feature.required: true") == {"feature.required": True}
    assert pr_args.handle_var_directive("/var feature.count: 3") == {"feature.count": 3}
    assert pr_args.handle_var_directive("/var feature.name: 'false'") == {"feature.name": "false"}


def test_handle_var_directive_preserves_yaml_collection_types() -> None:
    assert pr_args.handle_var_directive("/var runtime.benchmark_key: [short, multi-turn]") == {
        "runtime.benchmark_key": ["short", "multi-turn"]
    }
    assert pr_args.handle_var_directive("/var model_cache.pvc: {size: 300Gi}") == {
        "model_cache.pvc": {"size": "300Gi"}
    }


def test_parse_directives_preserves_var_types_through_yaml_roundtrip() -> None:
    config, directives = pr_args.parse_directives(
        """
/test fournos llm_d smoke
/var cleanup.enabled: false
/var runtime.benchmark_key: [short, multi-turn]
"""
    )

    assert directives == [
        "/test fournos llm_d smoke",
        "/var cleanup.enabled: false",
        "/var runtime.benchmark_key: [short, multi-turn]",
    ]
    assert config["cleanup.enabled"] is False
    assert config["runtime.benchmark_key"] == ["short", "multi-turn"]

    roundtripped = yaml.safe_load(yaml.safe_dump(config))
    assert roundtripped["cleanup.enabled"] is False
    assert roundtripped["runtime.benchmark_key"] == ["short", "multi-turn"]


def test_handle_var_directive_rejects_non_mapping_yaml() -> None:
    with pytest.raises(Exception, match="expected 'key: value'"):
        pr_args.handle_var_directive("/var false")
