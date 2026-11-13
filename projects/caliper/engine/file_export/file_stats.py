"""File count and size helpers for artifact export reporting."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def total_size(paths: list[Path]) -> int:
    """Return the total size in bytes of the given files (missing files count as 0)."""
    total = 0
    for p in paths:
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError as e:
            logger.warning("Could not stat %s: %s", p, e)
    return total


def human_size(num_bytes: int) -> str:
    """Format a byte count as a human-readable string (e.g. ``"1.4 GiB"``)."""
    size = float(num_bytes)
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_SIZE_UNITS[-1]}"


def describe_files(paths: list[Path]) -> str:
    """Describe a file list as ``"1426 files (1.2 GiB)"`` for log messages."""
    return f"{len(paths)} files ({human_size(total_size(paths))})"
