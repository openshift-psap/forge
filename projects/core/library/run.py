import logging
import os
import signal
import subprocess

logger = logging.getLogger(__name__)


def init():
    signal.signal(signal.SIGINT, raise_signal)
    signal.signal(signal.SIGTERM, raise_signal)

    # create new process group, become its leader,
    # except if we're already pid 1 (defacto group leader, setpgrp
    # gets permission denied error)
    if os.getpid() != 1:
        try:
            os.setpgrp()
        except Exception as e:
            logger.warning(f"Cannot call os.setpgrp: {e}")


class SignalInterrupt(SystemExit):
    def __init__(self, sig, frame):
        self.sig = sig
        self.frame = frame

    def __str__(self):
        return f"SignalInterrupt(sig={self.sig})"


def raise_signal(sig, frame):
    raise SignalInterrupt(sig, frame)


def run(
    command,
    capture_stdout=False,
    capture_stderr=False,
    check=True,
    protect_shell=True,
    cwd=None,
    stdin_file=None,
    log_command=True,
    decode_stdout=True,
    decode_stderr=True,
    timeout=None,
    env=None,
    handled_securely=False,
):
    if handled_securely:
        log_command = False

    if log_command:
        logger.info(f"run: {command}")

    args = {}

    args["cwd"] = cwd
    args["shell"] = True

    if env is not None:
        args["env"] = env

    if capture_stdout:
        args["stdout"] = subprocess.PIPE
    if capture_stderr:
        args["stderr"] = subprocess.PIPE
    if check:
        args["check"] = True
    if stdin_file:
        if not hasattr(stdin_file, "fileno"):
            raise ValueError("Argument 'stdin_file' must be an open file (with a file descriptor)")
        args["stdin"] = stdin_file

    if timeout is not None:
        args["timeout"] = timeout

    if protect_shell:
        command = f"set -o errexit;set -o pipefail;set -o nounset;set -o errtrace;{command}"

    try:
        proc = subprocess.run(command, **args)
    except subprocess.CalledProcessError as e:
        if handled_securely:
            raise subprocess.CalledProcessError(
                e.returncode,
                "<command hidden for security>",
                stderr=e.stderr,
            ) from None
        raise
    except subprocess.TimeoutExpired as e:
        if handled_securely:
            raise subprocess.TimeoutExpired("<command hidden for security>", e.timeout) from None
        raise

    if capture_stdout and decode_stdout:
        proc.stdout = proc.stdout.decode("utf8")
    if capture_stderr and decode_stderr:
        proc.stderr = proc.stderr.decode("utf8")

    return proc


def run_and_catch(exc, fct, *args, **kwargs):
    """
    Helper function for chaining multiple functions without swallowing exceptions
    Example:

    exc = None
    exc = run.run_and_catch(
      exc,
      run.run_toolbox, "kserve", "capture_operators_state", run_kwargs=dict(capture_stdout=True),
    )

    exc = run.run_and_catch(
      exc,
      run.run_toolbox, "cluster", "capture_environment", run_kwargs=dict(capture_stdout=True),
    )

    if exc: raise exc
    """
    if not (exc is None or isinstance(exc, Exception)):
        raise ValueError(f"exc={exc} should be None or an Exception ({exc.__class__})")

    try:
        fct(*args, **kwargs)
    except Exception as e:
        logger.error(f"{e.__class__.__name__}: {e}")
        exc = exc or e
    return exc
