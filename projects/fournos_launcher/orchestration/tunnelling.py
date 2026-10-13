import atexit
import logging
import os
import signal
import subprocess
import tempfile
import time

import yaml

from projects.core.library import config, vault

logger = logging.getLogger(__name__)

PROBE_RETRIES = 30
PROBE_DELAY = 5
RECREATE_TUNNEL_INTERVAL = 5

_tunnel_proc = None


def _kill_tunnel_proc(proc):
    """Terminate the SSH process group and wait for it to exit."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    proc.wait()


def close_tunnel():
    global _tunnel_proc

    if _tunnel_proc is None:
        return

    logger.info("Closing SSH tunnel ...")
    _kill_tunnel_proc(_tunnel_proc)
    _tunnel_proc = None


def open_tunnel():
    """Open an SSH tunnel to the intlab K8s API endpoint.

    Sets os.environ["KUBECONFIG"] to point to the cluster via the tunnel.
    """

    vault_name = config.project.get_config("fournos.intlab.vault.name")
    local_port = config.project.get_config("fournos.intlab.tunnel.local_port")
    ssh_flags = config.project.get_config("fournos.intlab.tunnel.ssh_flags")

    private_key_path = vault.get_vault_content_path(vault_name, "bastion_ssh_private_key")
    bastion_ssh_host_path = vault.get_vault_content_path(vault_name, "bastion_ssh_host")
    cluster_api_endpoint_path = vault.get_vault_content_path(vault_name, "cluster_api_endpoint")
    kubeconfig_path = vault.get_vault_content_path(vault_name, "cluster_kubeconfig")

    cmd = (
        f"ssh {' '.join(ssh_flags)}"
        f" -i {private_key_path} $(cat {bastion_ssh_host_path})"
        f" -L {local_port}:$(cat {cluster_api_endpoint_path})"
        f" -N"
    )

    atexit.register(close_tunnel)
    _create_and_probe_tunnel(cmd, local_port)

    local_kubeconfig = _setup_kubeconfig(kubeconfig_path, local_port)
    os.environ["KUBECONFIG"] = local_kubeconfig
    logger.info(f"Kubeconfig set for localhost:{local_port}.")


def _create_and_probe_tunnel(cmd, local_port):
    """Start the SSH tunnel and probe until the endpoint is reachable."""

    recreate_countdown = RECREATE_TUNNEL_INTERVAL

    def _start():
        global _tunnel_proc

        logger.info("Starting SSH tunnel ...")
        _tunnel_proc = subprocess.Popen(cmd, shell=True, start_new_session=True)
        time.sleep(PROBE_DELAY)
        return _tunnel_proc

    proc = _start()

    logger.info("Waiting for the tunnel to be ready ...")
    for i in range(PROBE_RETRIES):
        if _probe_endpoint(local_port):
            logger.info("Tunnel is ready.")
            return proc

        recreate_countdown -= 1
        logger.info(f"Probe attempt {i + 1}/{PROBE_RETRIES} failed ...")

        if i == PROBE_RETRIES - 1:
            _kill_tunnel_proc(proc)
            raise RuntimeError(f"SSH tunnel probe failed after {PROBE_RETRIES} attempts")

        if recreate_countdown == 0:
            logger.info("Recreating SSH tunnel ...")
            _kill_tunnel_proc(proc)
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


def _setup_kubeconfig(vault_kubeconfig_path, local_port):
    """Copy the vault kubeconfig and rewrite the server URL to the tunnel endpoint."""

    kubeconfig_fd, kubeconfig_path = tempfile.mkstemp(prefix="kubeconfig-intlab-")
    os.close(kubeconfig_fd)

    with open(vault_kubeconfig_path) as f:
        kubeconfig = yaml.safe_load(f)

    for cluster in kubeconfig.get("clusters", []):
        cluster_conf = cluster.get("cluster", {})
        cluster_conf["server"] = f"https://localhost:{local_port}"

    with open(kubeconfig_path, "w") as f:
        yaml.dump(kubeconfig, f, default_flow_style=False)

    os.chmod(kubeconfig_path, 0o600)

    def _cleanup():
        try:
            os.remove(kubeconfig_path)
        except FileNotFoundError:
            pass

    atexit.register(_cleanup)

    return kubeconfig_path
