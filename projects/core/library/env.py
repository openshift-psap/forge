import logging
import os
import pathlib
import threading
import time

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

    return globals()[name]


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

        artifact_dir = env_forge_base_dir / f"forge_{time.strftime('%Y%m%d-%H%M')}"

        artifact_dir.mkdir(parents=True, exist_ok=True)
        os.environ["ARTIFACT_DIR"] = str(artifact_dir)

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

    ci_metadata_dir = ci_lib.get_ci_metadata_dir()
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
