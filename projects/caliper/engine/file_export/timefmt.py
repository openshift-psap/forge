"""Time and duration formatting helpers for file export logs."""

from __future__ import annotations

from datetime import datetime


def format_time() -> str:
    """Return the current wall-clock time as HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")


def format_duration(seconds: float) -> str:
    """Format a duration in seconds for logs (e.g. '12.3s', '3m05s', '1h02m03s')."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    if hours > 0:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes > 0:
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"
