"""Observed praxis platform state, loaded from captured K8s artifacts.

The 500-cliff report must state what the cluster actually ran with -- the
annotated ext_proc budget as enforced by the gateway's Envoy circuit breakers,
and the identity-metrics flag on the AuthPolicy and its generated AuthConfigs --
never what the harness configuration asked for. Both live in the artifacts of
the capture_maas_praxis command (run per test as a finalizer, and on every
enable_maas_praxis toggle).

Newer runs carry a ready-made praxis_state_summary.json (written by the capture
command itself); older campaigns only have the raw captured files, from which
the same summaries are derived here. When even those are absent the loader
returns None and the report states that the platform state was not captured.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from projects.ag_praxis.toolbox.capture_maas_praxis.main import (
    summarize_authorino,
    summarize_extproc_budget,
    summarize_identity_metrics,
    summarize_wasm_services,
)
from projects.caliper.engine.model import BaseTestNode

logger = logging.getLogger(__name__)

STATE_SUMMARY_FILENAME = "praxis_state_summary.json"
CAPTURE_DIR_MARKER = "__capture_maas_praxis"


@dataclass(frozen=True)
class PraxisK8sState:
    """Observed praxis configuration of one test node, from captured artifacts."""

    extproc_max_requests: dict
    identity_metrics: dict
    authorino: dict | None
    wasm_services: dict | None = None
    from_summary: bool = False
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
    """Load the observed praxis state of a test node from its capture artifacts.

    Prefers the newest capture (the test finalizer's, numbered after the one
    from the enable toggle), and falls back to deriving the summaries from the
    raw captured files for campaigns that predate the summary file.
    """
    summary_path = _find_capture_artifact(test_node, STATE_SUMMARY_FILENAME)
    if summary_path is not None:
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse %s: %s", summary_path, exc)
        else:
            return PraxisK8sState(
                extproc_max_requests=data.get("extproc_max_requests") or {},
                identity_metrics=data.get("identity_metrics") or {},
                authorino=data.get("authorino"),
                wasm_services=data.get("wasm_services"),
                from_summary=True,
                capture_dir=summary_path.parent,
            )

    tenant_path = _find_capture_artifact(test_node, "maastenantconfig.yaml")
    policies_path = _find_capture_artifact(test_node, "authpolicies.yaml")
    if tenant_path is None and policies_path is None:
        return None

    artifacts = (tenant_path or policies_path).parent
    return PraxisK8sState(
        extproc_max_requests=summarize_extproc_budget(artifacts),
        identity_metrics=summarize_identity_metrics(artifacts),
        authorino=summarize_authorino(artifacts),
        wasm_services=summarize_wasm_services(artifacts),
        from_summary=False,
        capture_dir=artifacts,
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
