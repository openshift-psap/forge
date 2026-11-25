"""Tests for the kuadrant wasm shim configuration helpers.

Covers extract_wasm_shim_services (content-based discovery of the shim
"services" config in an EnvoyFilter spec or a listener config_dump) and the
auth-timeout patch-entry walk of enable_maas_praxis.set_auth_timeout: both
locate a JSON string embedded at an unstable path, so they are tested against
realistic layouts -- including the kuadrant-generated EnvoyFilter shape
observed on kpouget-llmd / fournOS-fg (2026-11-24).
"""

from __future__ import annotations

import json

from projects.ag_praxis.toolbox.capture_maas_praxis.main import (
    WASM_SHIM_AUTH_SERVICE,
    extract_wasm_shim_services,
)
from projects.ag_praxis.toolbox.enable_maas_praxis.main import (
    _contains_wasm_shim_config,
)

SHIM_CONFIG = {
    "actionSets": [
        {
            "actions": [
                {
                    "predicates": ['request.path != "/maas-api/health"'],
                    "service": "auth-service",
                }
            ]
        }
    ],
    "services": {
        "auth-service": {
            "endpoint": "kuadrant-auth-service",
            "failureMode": "deny",
            "timeout": "2s",
            "type": "auth",
        },
        "ratelimit-check-service": {
            "endpoint": "kuadrant-ratelimit-service",
            "failureMode": "deny",
            "timeout": "100ms",
            "type": "ratelimit-check",
        },
    },
}

# The EnvoyFilter spec shape written by the kuadrant operator: the shim
# configuration is a compact JSON string under configPatches -> typed_config.
ENVOYFILTER_SPEC = {
    "configPatches": [
        {
            "applyTo": "HTTP_FILTER",
            "patch": {
                "value": {
                    "name": "envoy.filters.http.wasm",
                    "typed_config": {
                        "@type": "type.googleapis.com/udpa.type.v1.TypedStruct",
                        "type_url": "type.googleapis.com/envoy.extensions.filters.http.wasm.v3.Wasm",
                        "value": {
                            "config": {
                                "name": "kuadrant-wasm-shim",
                                "root_id": "kuadrant_wasm_shim",
                                "configuration": {
                                    "@type": "type.googleapis.com/google.protobuf.StringValue",
                                    "value": json.dumps(SHIM_CONFIG, separators=(",", ":")),
                                },
                            }
                        },
                    },
                },
            },
        },
        {
            "applyTo": "CLUSTER",
            "patch": {"value": {"name": "kuadrant-operator-wasm"}},
        },
    ]
}

# The gateway's dynamic_listeners config_dump shape: the same configuration
# string reachable through nested listener/filter_chain structures.
LISTENER_CONFIG_DUMP = {
    "configs": [
        {
            "dynamic_listeners": [
                {
                    "active_state": {
                        "listener": {
                            "filter_chains": [
                                {
                                    "filters": [
                                        {
                                            "name": "envoy.filters.network.http_connection_manager",
                                            "typed_config": {
                                                "http_filters": [
                                                    {
                                                        "name": "envoy.filters.http.wasm",
                                                        "typed_config": {
                                                            "value": {
                                                                "config": {
                                                                    "configuration": {
                                                                        "value": json.dumps(
                                                                            SHIM_CONFIG,
                                                                            separators=(",", ":"),
                                                                        )
                                                                    }
                                                                }
                                                            }
                                                        },
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            ]
        }
    ]
}


def test_extract_services_from_envoyfilter_spec():
    services = extract_wasm_shim_services({"spec": ENVOYFILTER_SPEC})

    assert services == SHIM_CONFIG["services"]


def test_extract_services_from_listener_config_dump():
    services = extract_wasm_shim_services(LISTENER_CONFIG_DUMP)

    assert services is not None
    assert services[WASM_SHIM_AUTH_SERVICE]["timeout"] == "2s"


def test_extract_services_absent():
    assert extract_wasm_shim_services({"spec": {"configPatches": []}}) is None
    assert extract_wasm_shim_services({}) is None
    assert extract_wasm_shim_services(None) is None


def test_extract_services_ignores_other_json_strings():
    document = {"spec": {"configPatches": [{"patch": {"value": json.dumps({"x": 1})}}]}}

    assert extract_wasm_shim_services(document) is None


def test_contains_wasm_shim_config():
    assert _contains_wasm_shim_config({"spec": ENVOYFILTER_SPEC})
    assert _contains_wasm_shim_config(LISTENER_CONFIG_DUMP)
    assert not _contains_wasm_shim_config({"spec": {}})
    assert not _contains_wasm_shim_config(None)


def test_auth_timeout_patch_roundtrip():
    """The patched configuration string re-parses with only the timeout changed."""
    config = json.loads(json.dumps(SHIM_CONFIG))
    config["services"][WASM_SHIM_AUTH_SERVICE]["timeout"] = "5s"
    patched = json.dumps(config, separators=(",", ":"))

    services = extract_wasm_shim_services({"value": patched})

    assert services[WASM_SHIM_AUTH_SERVICE]["timeout"] == "5s"
    assert services["ratelimit-check-service"]["timeout"] == "100ms"
