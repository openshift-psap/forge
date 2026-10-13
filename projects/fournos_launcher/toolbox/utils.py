"""
Utilities for the FOURNOS launcher toolbox.
"""

import re


def validate_ttl(ttl: str) -> bool:
    """
    Validate TTL (Time to Live) format for FOURNOS jobs.

    Accepts formats like:
    - "30m" (30 minutes)
    - "2h" (2 hours)
    - "1d" (1 day)
    - "1h30m" (1 hour 30 minutes)
    - "2d12h" (2 days 12 hours)

    Args:
        ttl: TTL string to validate

    Returns:
        bool: True if valid, False otherwise

    Examples:
        >>> validate_ttl("12h")
        True
        >>> validate_ttl("30m")
        True
        >>> validate_ttl("1d12h30m")
        True
        >>> validate_ttl("invalid")
        False
    """
    if not isinstance(ttl, str):
        return False

    # TTL regex pattern: supports combinations of days, hours, minutes
    # Pattern breakdown:
    # ^                     - start of string
    # (?:                   - non-capturing group for full pattern
    #   (?:\d+d)?           - optional: one or more digits followed by 'd' (days)
    #   (?:\d+h)?           - optional: one or more digits followed by 'h' (hours)
    #   (?:\d+m)?           - optional: one or more digits followed by 'm' (minutes)
    # )
    # $                     - end of string
    #
    # The + after the main group ensures at least one time unit is present
    ttl_pattern = r"^(?:(?:\d+d)?(?:\d+h)?(?:\d+m)?)+$"

    if not re.match(ttl_pattern, ttl.strip()):
        return False

    # Additional check: ensure at least one time unit is actually present
    # (the regex allows empty string, but we need at least one unit)
    units = re.findall(r"\d+[dhm]", ttl.strip())
    if not units:
        return False

    # Check for duplicate units (e.g., "1h2h" should be invalid)
    unit_types = [unit[-1] for unit in units]  # Extract the unit type (d, h, m)
    if len(unit_types) != len(set(unit_types)):
        return False

    return True


# TTL validation regex pattern (for reference in config.yaml)
TTL_REGEX_PATTERN = r"^(?:(?:\d+d)?(?:\d+h)?(?:\d+m)?)+$"
