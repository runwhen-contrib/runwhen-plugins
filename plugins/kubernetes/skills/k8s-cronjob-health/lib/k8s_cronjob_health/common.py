"""Time and bounding helpers shared by the k8s-cronjob-health modules.

Pure functions over already-parsed Kubernetes JSON: no kubectl, no clock.
"""

import re
from datetime import datetime, timedelta, timezone

_DURATION = re.compile(r"(\d+)\s*(ms|[smhdw])")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta. Anything else raises."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise ValueError(f"not a duration: {text!r}")
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in parts))


def parse_time(value):
    """RFC 3339 string (as the Kubernetes API writes it) -> aware datetime, or None."""
    if not value:
        return None
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def seconds_between(start, end):
    """Whole seconds from start to end, or None when either is missing."""
    if start is None or end is None:
        return None
    return int((end - start).total_seconds())


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


# Bounds on what a single entry carries.
MAX_NESTED = 50
MAX_TEXT = 1024


def trunc(text):
    """(text cut to MAX_TEXT characters, whether it was cut). None stays None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:MAX_TEXT], True) if len(text) > MAX_TEXT else (text, False)


def nested(items):
    """(first MAX_NESTED items, whether any were dropped)."""
    items = list(items)
    return items[:MAX_NESTED], len(items) > MAX_NESTED
