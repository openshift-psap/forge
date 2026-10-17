"""
Utilities for the smoke request toolbox module.
"""

from __future__ import annotations

from typing import Any


def render_smoke_request_pod_from_parts(
    *,
    namespace: str,
    pod_name: str,
    client_image: str,
    api_key_secret: str = "",
) -> dict[str, Any]:
    """Render a smoke request pod manifest that sleeps forever.

    Args:
        namespace: Target namespace
        pod_name: Name for the smoke test pod
        client_image: Container image for making HTTP requests
        api_key_secret: Name of a K8s Secret to mount as an API key.

    Returns:
        Pod manifest as dict
    """
    container = {
        "name": "smoke",
        "image": client_image,
        "command": ["sleep", "infinity"],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }

    volumes = []

    if api_key_secret:
        container["volumeMounts"] = [
            {
                "name": "auth-token",
                "mountPath": "/secrets/auth-token",
                "readOnly": True,
            }
        ]
        volumes.append(
            {
                "name": "auth-token",
                "secret": {"secretName": api_key_secret},
            }
        )

    spec = {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 0,
        "containers": [container],
    }
    if volumes:
        spec["volumes"] = volumes

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/project": "llm_d",
                "forge.openshift.io/component": "smoke",
            },
        },
        "spec": spec,
    }
