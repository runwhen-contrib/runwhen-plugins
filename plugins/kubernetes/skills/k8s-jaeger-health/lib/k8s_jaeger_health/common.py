"""Bounds, durations and timestamps shared by the views."""

import re
from datetime import timedelta, timezone

from k8s_jaeger_health.errors import RwError

MESSAGE_LIMIT = 1024
NESTED_LIMIT = 50

_DURATION = re.compile(r"(\d+)\s*(ms|[smhdw])")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def cap(items, limit):
    return items[:limit]


def truncate(text, limit=MESSAGE_LIMIT):
    """(text cut to `limit` characters, whether it was cut); (None, False) for None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:limit], True) if len(text) > limit else (text, False)


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta; anything else is invalid_input."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise RwError(
            "invalid_input",
            f"not a duration: {str(text)[:64]!r} (use ms, s, m, h, d, w: 90s, 1h30m)",
        )
    delta = timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in parts))
    if delta <= timedelta(0):
        raise RwError("invalid_input", f"since must be longer than zero: {str(text)[:64]!r}")
    return delta


def iso(stamp):
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
