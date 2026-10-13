import logging
import os
import pathlib
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

# ARTIFACT_DIR is accessed via __getattr__ for thread-local support
BASE_ARTIFACT_DIR = None  # Immutable copy of the initial ARTIFACT_DIR
_GLOBAL_ARTIFACT_DIR = None  # Internal global fallback
FORGE_HOME = pathlib.Path(__file__).parents[3]

# Thread-local storage for ARTIFACT_DIR (thread-safe)
_tls_artifact_dir = threading.local()

# Global lock for artifact directory numbering to ensure sequential numbering in parallel execution
_artifact_dir_lock = threading.Lock()


def __getattr__(name):
    """Support thread-local ARTIFACT_DIR access."""
    if name == "ARTIFACT_DIR":
        # Each thread (including main) gets its own copy
        try:
            return _tls_artifact_dir.val
        except AttributeError:
            # Thread-local not set - this should not happen after proper initialization
            # Return global as emergency fallback but warn
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                f"Thread {threading.current_thread().name} accessing ARTIFACT_DIR without thread-local copy"
            )
            return globals().get("_GLOBAL_ARTIFACT_DIR")

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def get_tls_artifact_dir():
    """Get thread-local artifact directory."""
    try:
        return _tls_artifact_dir.val
    except AttributeError:
        # Thread-local not set - this should not happen after proper initialization
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Thread {threading.current_thread().name} has no thread-local ARTIFACT_DIR")
        return None


def _set_tls_artifact_dir(value):
    """Set thread-local artifact directory (thread-safe)."""
    _tls_artifact_dir.val = value


def ensure_thread_artifact_dir():
    """Ensure current thread has its own copy of ARTIFACT_DIR."""
    try:
        # Thread already has its own copy
        return _tls_artifact_dir.val
    except AttributeError:
        # Thread doesn't have a copy, inherit from global
        global_artifact_dir = globals().get("_GLOBAL_ARTIFACT_DIR")
        if global_artifact_dir is not None:
            _set_tls_artifact_dir(global_artifact_dir)
            return global_artifact_dir
        else:
            raise ValueError("No ARTIFACT_DIR available to copy to thread") from None


def _set_artifact_dir(value):
    global _GLOBAL_ARTIFACT_DIR
    _GLOBAL_ARTIFACT_DIR = value


def reset_artifact_dir():
    """Reset ARTIFACT_DIR to its original BASE_ARTIFACT_DIR value."""
    global _GLOBAL_ARTIFACT_DIR
    if BASE_ARTIFACT_DIR is not None:
        _GLOBAL_ARTIFACT_DIR = BASE_ARTIFACT_DIR
        os.environ["ARTIFACT_DIR"] = str(BASE_ARTIFACT_DIR)


def init(daily_artifact_dir=False):
    global ARTIFACT_DIR, BASE_ARTIFACT_DIR

    # Configure global logging to show INFO level messages
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        force=True,  # Override any existing basicConfig
    )

    if "ARTIFACT_DIR" in os.environ:
        artifact_dir = pathlib.Path(os.environ["ARTIFACT_DIR"])

    else:
        env_forge_base_dir = pathlib.Path(os.environ.get("FORGE_BASE_DIR", "/tmp"))

        # Try base format first, add seconds if directory exists
        base_name = f"forge_{time.strftime('%Y%m%d-%H%M')}"
        artifact_dir = env_forge_base_dir / base_name

        if artifact_dir.exists():
            # Directory exists, add seconds to make it unique
            unique_name = f"forge_{time.strftime('%Y%m%d-%H%M-%S')}"
            artifact_dir = env_forge_base_dir / unique_name

        artifact_dir.mkdir(parents=True, exist_ok=True)
        os.environ["ARTIFACT_DIR"] = str(artifact_dir)

        # Create or update forge_last symlink
        forge_last_link = env_forge_base_dir / "forge_last"
        if forge_last_link.exists() or forge_last_link.is_symlink():
            forge_last_link.unlink()
        forge_last_link.symlink_to(artifact_dir.name)

    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Set BASE_ARTIFACT_DIR to the initial value (immutable)
    if BASE_ARTIFACT_DIR is None:
        BASE_ARTIFACT_DIR = artifact_dir
        # Also expose it as an environment variable
        os.environ["FORGE_BASE_ARTIFACT_DIR"] = str(BASE_ARTIFACT_DIR)

    _set_artifact_dir(artifact_dir)
    # Also set in thread-local storage for main thread
    _set_tls_artifact_dir(artifact_dir)

    # Ensure CI metadata directory exists (lazy import to avoid circular imports)
    from . import ci as ci_lib

    ci_metadata_dir = ci_lib.get_ci_metadata_dir_location()
    ci_metadata_dir.mkdir(parents=True, exist_ok=True)


def NextArtifactDir(name, *, lock=None, counter_p=None):
    # Use global lock to ensure sequential numbering in parallel execution
    with _artifact_dir_lock:
        if lock:
            with lock:
                next_count = counter_p[0]
                counter_p[0] += 1
        else:
            next_count = next_artifact_index()

        # Use thread-local ARTIFACT_DIR for directory creation
        current_artifact_dir = None
        try:
            current_artifact_dir = _tls_artifact_dir.val
        except AttributeError:
            # Fallback to global if thread-local not set
            current_artifact_dir = globals().get("_GLOBAL_ARTIFACT_DIR")

        if current_artifact_dir is None:
            raise ValueError("ARTIFACT_DIR not set in either thread-local or global scope")

        dirname = current_artifact_dir / f"{next_count:03d}__{name}"

        # Create the TempArtifactDir which will mkdir in __init__
        return TempArtifactDir(dirname)


class TempArtifactDir:
    def __init__(self, dirname):
        self.dirname = pathlib.Path(dirname)
        self.previous_dirname = None
        # Create directory immediately to ensure proper numbering sequence
        self.dirname.mkdir(exist_ok=True)

    def __enter__(self):
        # Store current thread-local ARTIFACT_DIR
        try:
            self.previous_dirname = _tls_artifact_dir.val
        except AttributeError:
            # Fallback to global if thread-local not set
            self.previous_dirname = globals().get("_GLOBAL_ARTIFACT_DIR")

        # Only update environment variable in main thread to avoid parallel conflicts
        if threading.current_thread() == threading.main_thread():
            os.environ["ARTIFACT_DIR"] = str(self.dirname)
            # Set global for main thread compatibility
            _set_artifact_dir(self.dirname)

        # Always set thread-local (each thread gets its own)
        # Note: directory is already created in __init__
        _set_tls_artifact_dir(self.dirname)

        return True

    def __exit__(self, ex_type, ex_value, exc_traceback):
        # Only restore environment variable in main thread to avoid parallel conflicts
        if threading.current_thread() == threading.main_thread():
            os.environ["ARTIFACT_DIR"] = str(self.previous_dirname)
            # Restore global for main thread compatibility
            _set_artifact_dir(self.previous_dirname)

        # Always restore thread-local (each thread manages its own)
        _set_tls_artifact_dir(self.previous_dirname)

        return False  # If we returned True here, any exception would be suppressed!


def next_artifact_index():
    # Use thread-local ARTIFACT_DIR for counting
    current_artifact_dir = None
    try:
        current_artifact_dir = _tls_artifact_dir.val
    except AttributeError:
        # Fallback to global if thread-local not set
        current_artifact_dir = globals().get("_GLOBAL_ARTIFACT_DIR")

    if current_artifact_dir is None:
        return 0

    return len(list(current_artifact_dir.glob("*__*")))


def running_inside_fournos():
    return os.environ.get("FOURNOS_CI", "") == "true"


class MuteStdOut:
    """Context manager to mute stdout, stderr, and logging with a startup message."""

    def __init__(self, reason: str):
        self.reason = reason
        self.captured_stdout = StringIO()
        self.captured_stderr = StringIO()
        self.original_logging_level = None
        self.original_handlers_streams = []

    def __enter__(self):
        # Print the startup message before muting
        logger = logging.getLogger(__name__)
        logger.info(self.reason)

        # Redirect logging handlers that have direct stream references
        self._redirect_logging_handlers()

        # Suppress logging by setting root logger level to CRITICAL
        root_logger = logging.getLogger()
        self.original_logging_level = root_logger.level
        root_logger.setLevel(logging.CRITICAL)

        # Start capturing stdout and stderr
        self.stdout_redirect = redirect_stdout(self.captured_stdout)
        self.stderr_redirect = redirect_stderr(self.captured_stderr)

        self.stdout_redirect.__enter__()
        self.stderr_redirect.__enter__()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Restore stdout and stderr
        self.stdout_redirect.__exit__(exc_type, exc_val, exc_tb)
        self.stderr_redirect.__exit__(exc_type, exc_val, exc_tb)

        # Restore original logging level
        if self.original_logging_level is not None:
            root_logger = logging.getLogger()
            root_logger.setLevel(self.original_logging_level)

        # Restore original logging handler streams
        self._restore_logging_handlers()

        return False

    def _redirect_logging_handlers(self):
        """Redirect logging handlers with direct stream references to captured streams."""
        import sys

        # Save and redirect handlers from root logger and all existing loggers
        all_loggers = [logging.getLogger()] + [
            logging.getLogger(name) for name in logging.Logger.manager.loggerDict
        ]

        for logger in all_loggers:
            for handler in getattr(logger, "handlers", []):
                if hasattr(handler, "stream"):
                    original_stream = handler.stream
                    self.original_handlers_streams.append((handler, original_stream))

                    if original_stream is sys.stdout:
                        handler.stream = self.captured_stdout
                    elif original_stream is sys.stderr:
                        handler.stream = self.captured_stderr

    def _restore_logging_handlers(self):
        """Restore original streams to logging handlers."""
        for handler, original_stream in self.original_handlers_streams:
            handler.stream = original_stream
        self.original_handlers_streams.clear()
