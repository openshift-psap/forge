import atexit
import logging
import os
import subprocess
import tempfile
import time

from projects.core.library import config, run, vault
from projects.fournos_launcher.orchestration.utils import ensure_oc_available

logger = logging.getLogger(__name__)

PROBE_RETRIES = 30
PROBE_DELAY = 5
RECREATE_TUNNEL_INTERVAL = 5


def open_tunnel():
    """Open an SSH tunnel to the intlab K8s API endpoint and login via oc.

    Sets os.environ["KUBECONFIG"] to point to the new cluster.
    """

    vault_name = config.project.get_config("fournos.intlab.vault.name")
    local_port = config.project.get_config("fournos.intlab.tunnel.local_port")
    ssh_flags = config.project.get_config("fournos.intlab.tunnel.ssh_flags")

    private_key_path = vault.get_vault_content_path(vault_name, "bastion_ssh_private_key")
    bastion_ssh_host_path = vault.get_vault_content_path(vault_name, "bastion_ssh_host")
    cluster_api_endpoint_path = vault.get_vault_content_path(vault_name, "cluster_api_endpoint")
    login_user_path = vault.get_vault_content_path(vault_name, "cluster_login_user")
    login_password_path = vault.get_vault_content_path(vault_name, "cluster_login_password")

    cmd = (
        f"ssh {' '.join(ssh_flags)}"
        f" -i {private_key_path} $(cat {bastion_ssh_host_path})"
        f" -L {local_port}:$(cat {cluster_api_endpoint_path})"
        f" -N"
    )

    proc = _create_and_probe_tunnel(cmd, local_port)
    atexit.register(proc.kill)

    _oc_login(local_port, login_user_path, login_password_path)


def _create_and_probe_tunnel(cmd, local_port):
    """Start the SSH tunnel and probe until the endpoint is reachable."""

    recreate_countdown = RECREATE_TUNNEL_INTERVAL

    def _start():
        logger.info("Starting SSH tunnel ...")
        proc = subprocess.Popen(cmd, shell=True)
        time.sleep(PROBE_DELAY)
        return proc

    proc = _start()

    logger.info("Waiting for the tunnel to be ready ...")
    for i in range(PROBE_RETRIES):
        if _probe_endpoint(local_port):
            logger.info("Tunnel is ready.")
            return proc

        recreate_countdown -= 1
        logger.info(f"Probe attempt {i + 1}/{PROBE_RETRIES} failed ...")

        if i == PROBE_RETRIES - 1:
            proc.kill()
            raise RuntimeError(f"SSH tunnel probe failed after {PROBE_RETRIES} attempts")

        if recreate_countdown == 0:
            logger.info("Recreating SSH tunnel ...")
            proc.kill()
            proc = _start()
            recreate_countdown = RECREATE_TUNNEL_INTERVAL

        time.sleep(PROBE_DELAY)

    return proc


def _probe_endpoint(local_port):
    """Check if the K8s API is reachable through the tunnel."""
    try:
        subprocess.run(
            [
                "curl",
                "-sk",
                "--connect-timeout",
                "5",
                "--max-time",
                "5",
                f"https://localhost:{local_port}/healthz",
            ],
            capture_output=True,
            check=True,
            timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _oc_login(local_port, login_user_path, login_password_path):
    """Login to the cluster via oc and return the path to a tmp kubeconfig."""

    kubeconfig_fd, kubeconfig_path = tempfile.mkstemp(prefix="kubeconfig-intlab-")
    os.close(kubeconfig_fd)

    os.environ["KUBECONFIG"] = kubeconfig_path

    ensure_oc_available()

    try:
        run.run(
            f"oc login https://localhost:{local_port}"
            f" --insecure-skip-tls-verify"
            f" -u $(cat {login_user_path}) -p $(cat {login_password_path})",
            capture_stderr=True,
            handled_securely=True,
        )
    except subprocess.CalledProcessError as e:
        if e.stderr:
            logger.error(f"oc login failed: {e.stderr}")
        raise

    logger.info("oc login successful.")
