"""Formatting helpers used across services and tools.

These helpers are intentionally dependency-free so they can be imported from
any layer without creating cycles.
"""

from __future__ import annotations

import math
from typing import Iterable


_BYTE_UNITS: tuple[str, ...] = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def bytes_to_human(num_bytes: float, *, precision: int = 2) -> str:
    """Convert a byte count to a human-readable string.

    Examples:
        >>> bytes_to_human(0)
        '0 B'
        >>> bytes_to_human(1536)
        '1.50 KiB'
        >>> bytes_to_human(1024 ** 3 * 4.5)
        '4.50 GiB'

    Args:
        num_bytes: The size in bytes. Negative values are formatted with a
            leading minus sign.
        precision: Number of decimal places to keep for non-byte units.

    Returns:
        A human-readable string using binary (IEC) units.
    """
    if num_bytes is None or math.isnan(num_bytes):  # type: ignore[arg-type]
        return "unknown"

    sign = "-" if num_bytes < 0 else ""
    value = abs(float(num_bytes))
    if value < 1024:
        return f"{sign}{int(value)} B"

    exponent = min(int(math.log(value, 1024)), len(_BYTE_UNITS) - 1)
    scaled = value / (1024**exponent)
    return f"{sign}{scaled:.{precision}f} {_BYTE_UNITS[exponent]}"


def format_duration(seconds: float, *, precision: int = 2) -> str:
    """Format a duration expressed in seconds.

    Examples:
        >>> format_duration(0.023)
        '23.00 ms'
        >>> format_duration(1.5)
        '1.50 s'
        >>> format_duration(75)
        '1m 15.00s'
        >>> format_duration(3725)
        '1h 2m 5.00s'
    """
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return f"-{format_duration(-seconds, precision=precision)}"
    if seconds < 1:
        return f"{seconds * 1000:.{precision}f} ms"
    if seconds < 60:
        return f"{seconds:.{precision}f} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {sec:.{precision}f}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{int(hours)}h {int(minutes)}m {sec:.{precision}f}s"
    days, hours = divmod(hours, 24)
    return f"{int(days)}d {int(hours)}h {int(minutes)}m {sec:.{precision}f}s"


def truncate_text(text: str, *, max_chars: int = 4000, tail: bool = True) -> str:
    """Return ``text`` shortened to ``max_chars`` with a truncation marker.

    Args:
        text: The input string.
        max_chars: Maximum output length in characters (must be > 20).
        tail: When ``True``, keep the tail of the string (useful for logs);
            when ``False``, keep the head.

    Returns:
        Either the original text or a shortened version with a marker such as
        ``"... [truncated 12345 chars] ..."`` explaining what was cut.
    """
    if max_chars <= 20:
        raise ValueError("max_chars must be > 20 to leave room for the marker")
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text

    dropped = len(text) - max_chars + 40
    marker = f"\n... [truncated {dropped} chars] ...\n"
    keep = max_chars - len(marker)
    if tail:
        return marker + text[-keep:]
    return text[:keep] + marker


def join_nonempty(items: Iterable[str], separator: str = "\n") -> str:
    """Join ``items``, ignoring falsy elements. Handy when composing prompts."""
    return separator.join(item for item in items if item)


__all__ = [
    "bytes_to_human",
    "format_duration",
    "join_nonempty",
    "truncate_text",
]
