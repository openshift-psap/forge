from __future__ import annotations

import json
import logging
import os
import shlex
import tempfile

from projects.core.dsl import entrypoint, execute_tasks, task, user_notification
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
    returned via ctx.api_key_file_path, and its length in bytes via
    ctx.api_key_length (never the key itself).

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
    ctx.api_key_length = 0  # set by request_maas_token; default for callers

    token_cmd = f"oc create token {shlex.quote(args.sa_name)} -n {shlex.quote(args.namespace)}"
    # Safe to log: the SA token is only in the command's stdout, which
    # handled_secretly keeps out of the logs — the text carries no secret.
    logger.info(f"<safe command> {token_cmd}")

    token_result = oc(
        "create",
        "token",
        args.sa_name,
        "-n",
        args.namespace,
        handled_secretly=True,
    )
    sa_token = token_result.stdout.strip()

    copy_cmd = (
        f"oc -n {shlex.quote(args.deployer_namespace)} exec -i"
        f" {shlex.quote(args.deployer_pod)} -- sh -c {shlex.quote(f'cat > {TOKEN_PATH_IN_POD}')}"
    )
    # Safe to log: the SA token travels via stdin (input_text), never via the
    # command text.
    logger.info(f"<safe command> {copy_cmd}")

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
    # Safe to log: the SA token is referenced via $(cat ...), only substituted
    # inside the pod at runtime — it never appears in the command text.
    # The response (which contains the API key) must never be logged.
    logger.info(f"Requesting MaaS API key: {curl_cmd}")
    logger.info(f"<safe command> {curl_cmd}")

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

    api_key_length = len(api_key.encode())
    ctx.api_key_length = api_key_length
    logger.info(f"API key length: {api_key_length} bytes")

    # CONNLINK-2020: authorino's evaluator cache (freecache) silently refuses
    # entries larger than 1/1024 of its size — 1 kB at the default 1 MB. The
    # apiKeyValidation/auth-valid cache keys of maas-gateway-auth embed the raw
    # API key, so a key longer than ~1 kB disables those caches with no trace
    # in authorino's logs or metrics. Only the length is logged, never the key.
    if api_key_length > 1000:
        message = (
            f"The MaaS API key is {api_key_length} bytes long.\n"
            "\n"
            "Authorino's evaluator cache (freecache) silently refuses entries\n"
            "larger than 1/1024 of its size — 1 kB at the default 1 MB\n"
            "evaluatorCacheSize. The apiKeyValidation and auth-valid cache keys of\n"
            "the MaaS AuthPolicy embed the raw API key, so with a key this long\n"
            "those caches store nothing and every request pays the full\n"
            "authentication pipeline, with no trace in authorino's logs or metrics\n"
            "(CONNLINK-2020).\n"
            "\n"
            "Raise spec.evaluatorCacheSize on the Authorino CR to make the caches\n"
            "effective.\n"
        )
        user_notification.warning("maas-api-key-too-long", message)
        logger.warning(
            f"API key is {api_key_length} bytes: authorino cache entries are capped "
            f"at 1/1024 of evaluatorCacheSize (1 kB at the default 1 MB), so the "
            f"apiKeyValidation/auth-valid caches will silently not store anything "
            f"(CONNLINK-2020). Raise spec.evaluatorCacheSize on the Authorino CR."
        )

    return f"API key written to {fd[1]} (length: {api_key_length} bytes)"


if __name__ == "__main__":
    run.main()
