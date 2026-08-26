"""Parse / format human durations for the self-destruct feature.

Accepts compound forms like "1d2h30m", "45s", "90m", or "0" (disabled).
"""

from __future__ import annotations

import re

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_TOKEN = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)


def parse_duration(text: str) -> int | None:
    """Return seconds (>=0), or None if the text can't be parsed."""
    t = text.strip().lower()
    if t in ("0", "off", "none", "关闭", "关"):
        return 0
    total = 0
    matched = False
    for num, unit in _TOKEN.findall(t):
        total += int(num) * _UNITS[unit.lower()]
        matched = True
    if not matched:
        # bare number of seconds, e.g. "300"
        if t.isdigit():
            return int(t)
        return None
    return total


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "关闭"
    parts = []
    for unit, size in (("天", 86400), ("小时", 3600), ("分", 60), ("秒", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts)
