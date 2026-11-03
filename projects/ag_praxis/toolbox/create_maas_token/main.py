from __future__ import annotations

import json
import logging
import os
import shlex
import tempfile

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc, oc_exec

logger = logging.getLogger("DSL")

TOKEN_PATH_IN_POD = "/tmp/maas-sa-token"


@entrypoint
def run(
    *,
    namespace: str,
    gateway_url: str,
    curl_connect_to: str,
    subscription_name: str,
    deployer_namespace: str = "default",
    deployer_pod: str = "maas-deployer",
    sa_name: str = "default",
):
    """Create an ephemeral MaaS API key via the deployer pod.

    The API key is written to a local temp file. The file path is
    returned via ctx.api_key_file_path.

    Args:
        namespace: Namespace of the ServiceAccount used for authentication.
        gateway_url: MaaS gateway URL (https://hostname).
        curl_connect_to: curl --connect-to value (hostname:443:svc) for in-cluster routing.
        subscription_name: MaaS subscription name for the API key.
        deployer_namespace: Namespace where the deployer pod runs.
        deployer_pod: Name of the deployer pod.
        sa_name: ServiceAccount name to create the token for.
    """
    return execute_tasks(locals())


@task
def create_sa_token(args, ctx):
    """Create a ServiceAccount token and write it into the deployer pod."""
    token_result = oc(
        "create",
        "token",
        args.sa_name,
        "-n",
        args.namespace,
        handled_secretly=True,
    )
    sa_token = token_result.stdout.strip()

    oc(
        "-n",
        args.deployer_namespace,
        "exec",
        "-i",
        args.deployer_pod,
        "--",
        "sh",
        "-c",
        f"cat > {TOKEN_PATH_IN_POD}",
        input_text=sa_token,
        handled_secretly=True,
    )

    return "SA token created and copied to deployer pod"


@task
def request_maas_token(args, ctx):
    """Request an ephemeral authentication token from the MaaS gateway."""
    payload = json.dumps(
        {
            "name": "forge-test-key",
            "expiresIn": "1h",
            "ephemeral": True,
            "subscription": args.subscription_name,
        }
    )

    curl_cmd = (
        f"curl -sS -k -X POST --connect-to {shlex.quote(args.curl_connect_to)}"
        f" {shlex.quote(args.gateway_url + '/maas-api/v1/api-keys')}"
        f" -H 'Content-Type: application/json'"
        f' -H "Authorization: Bearer $(cat {TOKEN_PATH_IN_POD})"'
        f" -d {shlex.quote(payload)}"
    )
    result = oc_exec(
        "sh",
        "-c",
        curl_cmd,
        namespace=args.deployer_namespace,
        pod=args.deployer_pod,
        handled_secretly=True,
    )

    if not result.stdout.strip():
        raise RuntimeError(
            f"MaaS API key creation returned empty response "
            f"(url={args.gateway_url}/maas-api/v1/api-keys, "
            f"subscription={args.subscription_name})"
        )

    response = json.loads(result.stdout)
    api_key = response.get("key")
    if not api_key:
        raise RuntimeError("Failed to create MaaS API key: response did not contain 'key'")

    fd = tempfile.mkstemp(prefix="maas-api-key-", suffix=".key")
    os.write(fd[0], api_key.encode())
    os.close(fd[0])
    ctx.api_key_file_path = fd[1]

    return f"API key written to {fd[1]}"


if __name__ == "__main__":
    run.main()
