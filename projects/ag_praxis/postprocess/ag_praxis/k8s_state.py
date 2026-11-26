"""Observed praxis platform state, loaded from captured K8s artifacts.

The 500-cliff report must state what the cluster actually ran with -- the
annotated ext_proc budget as enforced by the gateway's Envoy circuit breakers,
the identity-metrics flag on the AuthPolicy and its generated AuthConfigs, and
the kuadrant wasm shim's service settings (the effective auth-service timeout)
-- never what the harness configuration asked for. All live in the
praxis_state_summary.json written by the capture_maas_praxis command (run per
test as a finalizer, and on every enable_maas_praxis toggle).

When no capture produced a summary the loader returns None and the report
states that the platform state was not captured.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from projects.caliper.engine.model import BaseTestNode

logger = logging.getLogger(__name__)

STATE_SUMMARY_FILENAME = "praxis_state_summary.json"
CAPTURE_DIR_MARKER = "__capture_maas_praxis"


@dataclass(frozen=True)
class PraxisK8sState:
    """Observed praxis configuration of one test node, from the capture summary."""

    extproc_max_requests: dict
    identity_metrics: dict
    authorino: dict | None
    wasm_services: dict | None = None
    capture_dir: Path | None = None

    def effective_budget(self) -> int | None:
        """The budget the gateway actually enforced (payload ext_proc clusters)."""
        effective = self.extproc_max_requests.get("effective") or {}
        values = {
            entry.get("max_requests")
            for name, data in effective.items()
            if "payload" in name
            for entry in data.get("limits", [])
        }
        if len(values) == 1:
            return values.pop()
        return None

    def auth_timeout(self) -> str | None:
        """The auth-service timeout the gateway actually ran with."""
        service = (self.wasm_services or {}).get("auth-service") or {}
        return service.get("timeout")


def load_praxis_k8s_state(test_node: BaseTestNode) -> PraxisK8sState | None:
    """Load the observed praxis state of a test node from its capture summary.

    Prefers the newest capture (the test finalizer's, numbered after the one
    from the enable toggle). Returns None when no capture produced a summary.
    """
    summary_path = _find_capture_artifact(test_node, STATE_SUMMARY_FILENAME)
    if summary_path is None:
        return None

    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse %s: %s", summary_path, exc)
        return None

    return PraxisK8sState(
        extproc_max_requests=data.get("extproc_max_requests") or {},
        identity_metrics=data.get("identity_metrics") or {},
        authorino=data.get("authorino"),
        wasm_services=data.get("wasm_services"),
        capture_dir=summary_path.parent,
    )


def _find_capture_artifact(test_node: BaseTestNode, filename: str) -> Path | None:
    """Newest captured file with this name produced by capture_maas_praxis."""
    matches = []
    for artifact_path in test_node.artifact_paths:
        path = Path(artifact_path)
        if path.name != filename:
            continue
        if not any(CAPTURE_DIR_MARKER in part for part in path.parts):
            continue
        matches.append(path)

    if not matches:
        return None
    return sorted(matches)[-1]
