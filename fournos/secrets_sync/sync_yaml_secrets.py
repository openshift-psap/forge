#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "kubernetes",
#     "pyyaml",
# ]
# ///
"""Synchronize secrets between a YAML file and Kubernetes.

Provides two subcommands:

``extract``
    Read vault-entry-labelled Secrets from the cluster and write them
    as a plain-text YAML file (suitable for importing into a vault
    manager).

``sync``
    Read a YAML file and create/update Kubernetes Opaque Secrets,
    deleting any managed secrets not present in the file.

YAML format
-----------
Top-level keys are entry names (the values used in FournosJob
``secretRefs``).  Nested keys are the secret data::

    my-creds:
      username: example-user
      password: example-pass
    another-entry:
      api-key: example-key

Optional environment variables
------------------------------
* ``FOURNOS_SECRETS_NAMESPACE`` — target Kubernetes namespace (can also
  use ``-n``). Defaults to ``psap-secrets``.

Examples
--------
Extract current cluster secrets to a file::

    python hacks/sync_yaml_secrets.py extract -o secrets.yaml

Sync a YAML file to the cluster::

    python hacks/sync_yaml_secrets.py sync secrets.yaml

Preview without touching the cluster::

    python hacks/sync_yaml_secrets.py sync secrets.yaml --dry-run
"""

from __future__ import annotations

import argparse
import base64
import logging
import re
import sys

import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

NAMESPACE_DEFAULT = "psap-secrets"

LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
ANNOTATION_YAML_SOURCE = "fournos.dev/yaml-source"
MANAGER_VALUE = "fournos-vault-sync"

LABEL_VAULT_ENTRY = "fournos.dev/vault-entry"
VAULT_SECRET_PATTERN = "vault-{entry}"
DEFAULT_YAML_FILE = "secrets.yaml"

# ---------------------------------------------------------------------------
# Kubernetes helpers (lazy import so --dry-run works without kubeconfig)
# ---------------------------------------------------------------------------


def _k8s_core_api():
    from kubernetes import client
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_kube_config()
    except k8s_config.ConfigException:
        k8s_config.load_incluster_config()
    return client.CoreV1Api()


def _apply_secret(
    v1,
    name: str,
    namespace: str,
    data: dict[str, str],
    yaml_source: str,
):
    from kubernetes.client import V1ObjectMeta, V1Secret
    from kubernetes.client.exceptions import ApiException

    secret = V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                LABEL_MANAGED_BY: MANAGER_VALUE,
                LABEL_VAULT_ENTRY: "true",
            },
            annotations={
                ANNOTATION_YAML_SOURCE: yaml_source,
            },
        ),
        type="Opaque",
        string_data=data,
    )

    try:
        v1.read_namespaced_secret(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        v1.create_namespaced_secret(namespace, secret)
        logger.info("Created  secret/%s -n %s", name, namespace)
    else:
        v1.replace_namespaced_secret(name, namespace, secret)
        logger.info("Updated  secret/%s -n %s", name, namespace)


def _list_managed_secrets(v1, namespace: str) -> list[str]:
    from kubernetes.client.exceptions import ApiException

    try:
        label_selector = f"{LABEL_VAULT_ENTRY}=true"
        secrets = v1.list_namespaced_secret(namespace, label_selector=label_selector)
        return [secret.metadata.name for secret in secrets.items]
    except ApiException as exc:
        logger.error("Failed to list managed secrets: %s", exc)
        raise


def _delete_secret(v1, name: str, namespace: str):
    from kubernetes.client.exceptions import ApiException

    try:
        v1.delete_namespaced_secret(name, namespace)
        logger.info("Deleted  secret/%s -n %s", name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            logger.error("Failed to delete secret/%s -n %s: %s", name, namespace, exc)
            raise


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(r"^[-._a-zA-Z0-9]+$")
_DNS_1123_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$")
_DNS_1123_MAX_LEN = 253


def _is_valid_k8s_key(key: str) -> bool:
    return bool(key) and len(key) <= 253 and bool(_SECRET_KEY_RE.match(key))


def is_valid_k8s_name(name: str) -> bool:
    return bool(name) and len(name) <= _DNS_1123_MAX_LEN and bool(_DNS_1123_RE.match(name))


def _entry_name_from_secret(secret_name: str) -> str | None:
    """Reverse ``settings.vault_secret_pattern`` to recover the entry name."""

    regex = "^" + re.escape(VAULT_SECRET_PATTERN).replace(r"\{entry\}", r"(?P<entry>.+)") + "$"
    m = re.match(regex, secret_name)
    return m.group("entry") if m else None


# ---------------------------------------------------------------------------
# extract subcommand
# ---------------------------------------------------------------------------


def extract(
    *,
    namespace: str,
    output: str | None,
) -> int:
    v1 = _k8s_core_api()

    label_selector = f"{LABEL_VAULT_ENTRY}=true"
    secrets = v1.list_namespaced_secret(namespace, label_selector=label_selector)

    if not secrets.items:
        logger.warning("No vault-entry secrets found in namespace %s", namespace)
        return 0

    logger.info("Found %d vault-entry secrets in %s", len(secrets.items), namespace)

    result: dict[str, dict[str, str]] = {}
    for secret in secrets.items:
        name = secret.metadata.name
        entry = _entry_name_from_secret(name)
        if entry is None:
            logger.warning(
                "Secret %s does not match pattern %r, using raw name",
                name,
                VAULT_SECRET_PATTERN,
            )
            entry = name

        decoded: dict[str, str] = {}
        for k, v in (secret.data or {}).items():
            try:
                decoded[k] = base64.b64decode(v).decode("utf-8")
            except Exception:
                logger.warning("Cannot decode key %s in secret %s, skipping", k, name)
                continue

        if decoded:
            result[entry] = decoded
            logger.info("  %s: %d key(s)", entry, len(decoded))

    def _str_representer(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    dumper = yaml.Dumper
    dumper.add_representer(str, _str_representer)
    yaml_text = yaml.dump(
        result, Dumper=dumper, default_flow_style=False, sort_keys=True, width=2**31
    )

    if output:
        with open(output, "w") as f:
            f.write(yaml_text)
        logger.info("Written to %s", output)
        logger.warning(
            "WARNING: %s contains plain-text secrets. Delete it when you are done!",
            output,
        )
    else:
        sys.stdout.write(yaml_text)

    return 0


# ---------------------------------------------------------------------------
# diff subcommand
# ---------------------------------------------------------------------------


def _read_cluster_secrets(v1, namespace: str) -> dict[str, dict[str, str]]:
    """Read all vault-entry secrets from the cluster, returning {entry_name: {key: value}}."""
    label_selector = f"{LABEL_VAULT_ENTRY}=true"
    secrets = v1.list_namespaced_secret(namespace, label_selector=label_selector)

    result: dict[str, dict[str, str]] = {}
    for secret in secrets.items:
        name = secret.metadata.name
        entry = _entry_name_from_secret(name)
        if entry is None:
            entry = name

        decoded: dict[str, str] = {}
        for k, v in (secret.data or {}).items():
            try:
                decoded[k] = base64.b64decode(v).decode("utf-8")
            except Exception:
                decoded[k] = "<binary>"
        result[entry] = decoded
    return result


def diff(
    *,
    yaml_file: str,
    namespace: str,
) -> int:
    with open(yaml_file) as f:
        yaml_data = yaml.safe_load(f)

    if not isinstance(yaml_data, dict):
        logger.error("YAML file must be a mapping of entry names to key-value pairs")
        return 1

    v1 = _k8s_core_api()
    cluster_data = _read_cluster_secrets(v1, namespace)

    yaml_entries = set(yaml_data.keys())
    cluster_entries = set(cluster_data.keys())

    has_diff = False

    missing_in_cluster = yaml_entries - cluster_entries
    if missing_in_cluster:
        has_diff = True
        for entry in sorted(missing_in_cluster):
            keys = sorted(yaml_data[entry].keys()) if isinstance(yaml_data[entry], dict) else []
            print(f"+ secret not found in cluster: {entry}")
            for k in keys:
                print(f"  + {k}")
            print()

    extra_in_cluster = cluster_entries - yaml_entries
    if extra_in_cluster:
        has_diff = True
        for entry in sorted(extra_in_cluster):
            keys = sorted(cluster_data[entry].keys())
            print(f"- extra secret in cluster (not in YAML): {entry}")
            for k in keys:
                print(f"  - {k}")

    common = yaml_entries & cluster_entries
    for entry in sorted(common):
        yaml_kv = yaml_data[entry] if isinstance(yaml_data[entry], dict) else {}
        cluster_kv = cluster_data[entry]

        yaml_keys = set(yaml_kv.keys())
        cluster_keys = set(cluster_kv.keys())

        entry_diffs: list[str] = []

        for k in sorted(yaml_keys - cluster_keys):
            entry_diffs.append(f"  + field missing in cluster: {k}")

        for k in sorted(cluster_keys - yaml_keys):
            entry_diffs.append(f"  - extra field in cluster (not in YAML): {k}")

        for k in sorted(yaml_keys & cluster_keys):
            yaml_val = str(yaml_kv[k])
            cluster_val = cluster_kv[k]
            if yaml_val != cluster_val:
                entry_diffs.append(
                    f"  ~ field differs: {k} (<{len(yaml_val)} chars> yaml vs <{len(cluster_val)} chars> cluster)"
                )

        if entry_diffs:
            has_diff = True
            print(f"~ {entry}:")
            for line in entry_diffs:
                print(line)

    if not has_diff:
        print("No differences found.")

    return 0


# ---------------------------------------------------------------------------
# sync subcommand
# ---------------------------------------------------------------------------


def sync(
    *,
    yaml_file: str,
    namespace: str,
    dry_run: bool,
    allow_delete: bool = False,
) -> int:
    with open(yaml_file) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        logger.error("YAML file must be a mapping of entry names to key-value pairs")
        return 1

    if not data:
        logger.warning("YAML file is empty")
        return 0

    logger.info("Loaded %d entries from %s", len(data), yaml_file)

    v1 = _k8s_core_api()
    processed_secrets: set[str] = set()
    existing_managed_secrets = set(_list_managed_secrets(v1, namespace))
    logger.info("Found %d existing managed secrets", len(existing_managed_secrets))

    errors = 0
    for entry_name, kv_data in data.items():
        entry_name = str(entry_name)

        if not is_valid_k8s_name(entry_name):
            logger.error("Entry %r is not a valid DNS-1123 name, skipping", entry_name)
            errors += 1
            continue

        if not isinstance(kv_data, dict):
            logger.error("Entry %r value must be a mapping, skipping", entry_name)
            errors += 1
            continue

        secret_name = VAULT_SECRET_PATTERN.format(entry=entry_name)

        safe_data: dict[str, str] = {}
        for k, v in kv_data.items():
            if not _is_valid_k8s_key(str(k)):
                logger.error(
                    "Key %r in entry %s is not a valid K8s Secret key, skipping",
                    k,
                    entry_name,
                )
                errors += 1
                continue
            safe_data[str(k)] = str(v)

        if not safe_data:
            logger.warning("Entry %s has no valid keys, skipping", entry_name)
            continue

        logger.info(
            "  %s -> %s: %d key(s): %s",
            entry_name,
            secret_name,
            len(safe_data),
            ", ".join(safe_data.keys()),
        )

        if dry_run:
            print(f"[dry-run] Would create/update secret/{secret_name} -n {namespace}")
            for key, value in safe_data.items():
                print(f"  {key}: <{len(str(value))} chars>")
            processed_secrets.add(secret_name)
            continue

        try:
            _apply_secret(
                v1,
                secret_name,
                namespace,
                safe_data,
                yaml_source=yaml_file,
            )
            processed_secrets.add(secret_name)
        except Exception:
            logger.exception(
                "Failed to apply secret/%s -n %s",
                secret_name,
                namespace,
            )
            errors += 1

    secrets_to_delete = existing_managed_secrets - processed_secrets
    if secrets_to_delete:
        logger.info(
            "Found %d managed secrets not in YAML: %s",
            len(secrets_to_delete),
            ", ".join(sorted(secrets_to_delete)),
        )
        if not dry_run and not allow_delete:
            try:
                answer = input("Delete these secrets? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer != "y":
                logger.info("Skipping deletion")
                secrets_to_delete = set()
        for secret_name in secrets_to_delete:
            if dry_run:
                print(f"[dry-run] Would delete secret/{secret_name} -n {namespace}")
            else:
                try:
                    _delete_secret(v1, secret_name, namespace)
                except Exception:
                    logger.exception(
                        "Failed to delete secret/%s -n %s",
                        secret_name,
                        namespace,
                    )
                    errors += 1
    else:
        logger.info("No managed secrets need to be deleted")

    return 1 if errors else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync secrets between a YAML file and Kubernetes.",
    )
    parser.add_argument(
        "-n",
        "--namespace",
        default=NAMESPACE_DEFAULT,
        help=f"Target Kubernetes namespace (default: {NAMESPACE_DEFAULT}).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # -- extract --
    p_extract = sub.add_parser(
        "extract",
        help="Extract cluster secrets to a YAML file.",
    )
    p_extract.add_argument(
        "-o",
        "--output",
        default=DEFAULT_YAML_FILE,
        help=f"Output file path (default: {DEFAULT_YAML_FILE}).",
    )

    # -- diff --
    p_diff = sub.add_parser(
        "diff",
        help="Show differences between a YAML file and the cluster.",
    )
    p_diff.add_argument(
        "yaml_file",
        nargs="?",
        default=DEFAULT_YAML_FILE,
        help=f"Path to the YAML secrets file (default: {DEFAULT_YAML_FILE}).",
    )

    # -- sync --
    p_sync = sub.add_parser(
        "sync",
        help="Sync a YAML file to the cluster.",
    )
    p_sync.add_argument(
        "yaml_file",
        nargs="?",
        default=DEFAULT_YAML_FILE,
        help=f"Path to the YAML secrets file (default: {DEFAULT_YAML_FILE}).",
    )
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created/updated/deleted without touching the cluster.",
    )
    p_sync.add_argument(
        "--allow-delete",
        action="store_true",
        help="Delete managed secrets not present in the YAML file without prompting.",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-5s %(message)s",
    )
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.command == "extract":
        return extract(
            namespace=args.namespace,
            output=args.output,
        )

    if args.command == "diff":
        return diff(
            yaml_file=args.yaml_file,
            namespace=args.namespace,
        )

    if args.command == "sync":
        return sync(
            yaml_file=args.yaml_file,
            namespace=args.namespace,
            dry_run=args.dry_run,
            allow_delete=args.allow_delete,
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
