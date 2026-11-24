"""Fetch pinned release steps and adapt their CI paths for the internal workspace."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import yaml

STEPS = {
    "conf": "ipi/conf/ipi-conf",
    "ibmcloud": "ipi/conf/ibmcloud/ipi-conf-ibmcloud",
    "credentials": "ipi/conf/ibmcloud/manual-creds/ipi-conf-ibmcloud-manual-creds",
    "install": "ipi/install/install/ipi-install-install",
    "destroy": "ipi/deprovision/deprovision/ipi-deprovision-deprovision",
}
ROOT = "ci-operator/step-registry/"

# Only paths used by the supported IBM/public-cluster branch are relocated.
# Exact occurrence counts fail closed if a new upstream revision changes them.
# Inactive AWS/vSphere/guest-ignition paths are deliberately NOT globally replaced.
ADAPTATIONS = {
    "install": (
        # Upstream's tag stripping turns repository@sha256:... into an invalid
        # repository@sha256@sha256:... pullspec. Our inputs are always digests.
        (
            'CUSTOM_OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE="${CUSTOM_OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE%:*}"@"$CUSTOM_PAYLOAD_DIGEST"',
            'CUSTOM_OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE="${CUSTOM_OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE%@*}"@"$CUSTOM_PAYLOAD_DIGEST"',
            1,
        ),
        ('--to="/tmp"', '--to="${FORGE_RUNTIME_DIR}"', 1),
        (
            'export INSTALLER_BINARY="/tmp/openshift-install"',
            'export INSTALLER_BINARY="${FORGE_RUNTIME_DIR}/openshift-install"',
            1,
        ),
        ("export HOME=/tmp", 'export HOME="${FORGE_RUNTIME_DIR}"', 1),
        ("dir=/tmp/installer", 'dir="${FORGE_RUNTIME_DIR}/installer"', 1),
        ("backup=/tmp/install-orig", 'backup="${FORGE_RUNTIME_DIR}/install-orig"', 1),
    ),
    "destroy": (
        (
            "cp /tmp/installer/.openshift_install.log",
            'cp "${FORGE_RUNTIME_DIR}/installer/.openshift_install.log"',
            1,
        ),
        (
            'cp -ar "${SHARED_DIR}" /tmp/installer',
            'cp -ar "${SHARED_DIR}" "${FORGE_RUNTIME_DIR}/installer"',
            1,
        ),
        ('--to="/tmp"', '--to="${FORGE_RUNTIME_DIR}"', 1),
        (
            'export INSTALLER_BINARY="/tmp/openshift-install"',
            'export INSTALLER_BINARY="${FORGE_RUNTIME_DIR}/openshift-install"',
            1,
        ),
        ('"/tmp/installer/metadata.json"', '"${FORGE_RUNTIME_DIR}/installer/metadata.json"', 1),
        ("--dir /tmp/installer", '--dir "${FORGE_RUNTIME_DIR}/installer"', 2),
        (
            "[[ -s /tmp/installer/quota.json ]]",
            '[[ -s "${FORGE_RUNTIME_DIR}/installer/quota.json" ]]',
            1,
        ),
        ("cp /tmp/installer/quota.json", 'cp "${FORGE_RUNTIME_DIR}/installer/quota.json"', 1),
    ),
}


def execution_script(source: Path, action: str) -> bytes:
    content = (source / f"{action}-commands.sh").read_text()
    for old, new, count in ADAPTATIONS[action]:
        if content.count(old) != count:
            raise RuntimeError(
                f"Pinned upstream {action} script has changed; review path adaptation"
            )
        content = content.replace(old, new)
    return content.encode()


def prepare(commit: str, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for name, stem in STEPS.items():
        for suffix in ("commands.sh", "ref.yaml"):
            url = f"https://raw.githubusercontent.com/openshift/release/{commit}/{ROOT}{stem}-{suffix}"
            with urllib.request.urlopen(url, timeout=60) as response:
                (destination / f"{name}-{suffix}").write_bytes(response.read())
    # Adapt once before installation; finally uses these same saved scripts.
    for action in ADAPTATIONS:
        (destination / f"{action}-commands.sh").write_bytes(execution_script(destination, action))
    (destination / "lease.sh").write_text(
        "# Forge uses its own account quota, outside OpenShift CI's lease service.\n"
        "lease__install_lease_eligible() { return 1; }\n"
        "lease__release() { return 0; }\n"
        "lease__acquire() { echo 'Unexpected CI lease request' >&2; return 1; }\n"
        "lease__cat() { return 1; }\n"
    )


def defaults(source: Path) -> dict[str, str]:
    values = {}
    for name in STEPS:
        document = yaml.safe_load((source / f"{name}-ref.yaml").read_text())
        for option in document["ref"].get("env", []):
            value = option.get("default", "")
            values[option["name"]] = str(value).lower() if isinstance(value, bool) else str(value)
    return values
