"""Bounds, durations and timestamps shared by the views."""

import re
from datetime import datetime, timezone

from k8s_prometheus_health.errors import RwError

MESSAGE_LIMIT = 1024
NESTED_LIMIT = 50

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_FRACTION = re.compile(r"(\.\d{6})\d+")
# scheme://[userinfo@]host/path?query -> scheme://host/path. The userinfo match is greedy: it
# runs to the last `@` before the first `/`, `?` or `#`, so a password that contains `@` goes too.
_URL = re.compile(r"(https?://)(?:[^\s/\"'?#]*@)?([^\s\"'?#]*)(?:[?#][^\s\"']*)?")


def cap(items, limit):
    return items[:limit]


def truncate(text, limit=MESSAGE_LIMIT):
    """(text cut to `limit` characters, whether it was cut); (None, False) for None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:limit], True) if len(text) > limit else (text, False)


def strip_urls(text):
    """`text` with the query string, fragment and userinfo removed from every http(s) URL in it
    (a scrape URL or error message can carry a token in either). A token inside the path is
    not recognisable and stays."""
    if text is None:
        return None
    return _URL.sub(lambda m: m.group(1) + m.group(2), str(text))


def parse_duration(text):
    """Seconds for a Prometheus or Go duration such as `15s`, `1m30s`, `500ms`; None if it is not
    one (an absent interval is a fact, not an error)."""
    if not isinstance(text, str) or not text:
        return None
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(float(n) * _UNITS[u] for n, u in parts)


def parse_time(text):
    """An aware datetime for an RFC 3339 timestamp (nanoseconds allowed), None for an empty or
    unset one (Go's zero time, year 1, means never)."""
    if not isinstance(text, str) or not text or text.startswith("0001-01-01"):
        return None
    text = _FRACTION.sub(r"\1", text)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def iso(stamp):
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else None


def age_seconds(stamp, now):
    """Seconds from `stamp` to `now` (negative if the clocks disagree), None when unknown."""
    return round((now - stamp).total_seconds(), 1) if stamp else None


def positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise RwError("invalid_input", f"{name} must be a positive number, got {value!r}")
    return value
