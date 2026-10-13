import logging
import os
import signal
import subprocess

logger = logging.getLogger(__name__)

_signal_callbacks = {signal.SIGINT: [], signal.SIGTERM: []}


def register_signal_callback(fn, *, sig=None):
    if sig is None:
        for sig_type, callbacks in _signal_callbacks.items():
            callbacks.append(fn)
            logger.info(
                f"Registered signal callback {fn.__qualname__} for {signal.Signals(sig_type).name}"
            )
    else:
        _signal_callbacks[sig].append(fn)
        logger.info(f"Registered signal callback {fn.__qualname__} for {signal.Signals(sig).name}")


def init():
    logger.info(f"Installing signal handlers for pid={os.getpid()} pgid={os.getpgrp()}")
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
    sig_name = signal.Signals(sig).name
    # Write to stderr immediately, before any import or env access
    import sys

    print(f"raise_signal: {sig_name} in pid={os.getpid()}", file=sys.stderr, flush=True)
    logger.info(f"Received {sig_name} in pid={os.getpid()}")

    from datetime import datetime

    from projects.core.library import env

    log_file = None
    if env.BASE_ARTIFACT_DIR and env.BASE_ARTIFACT_DIR.is_dir():
        log_file = env.BASE_ARTIFACT_DIR / f"{sig_name}_interrupted.txt"
    else:
        logger.warning("BASE_ARTIFACT_DIR not available, cannot write signal file")

    for cb in _signal_callbacks.get(sig, []):
        try:
            cb(sig, frame, log_file)
        except Exception:
            logger.exception(f"Signal callback {cb} failed")

    if log_file:
        with log_file.open("a") as f:
            f.write(
                f"{datetime.now()}: {__name__}.{raise_signal.__qualname__} {sig_name} handler\n"
            )

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
