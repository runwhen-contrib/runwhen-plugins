"""Bounds and timestamps shared by the views."""

import re
from datetime import datetime, timezone


MESSAGE_LIMIT = 1024
NESTED_LIMIT = 50

_FRACTION = re.compile(r"(\.\d{6})\d+")


def cap(items, limit):
    return items[:limit]


def truncate(text, limit=MESSAGE_LIMIT):
    """(text cut to `limit` characters, whether it was cut); (None, False) for None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:limit], True) if len(text) > limit else (text, False)


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


def unparseable(value):
    """Whether `value` is present but is not an RFC 3339 timestamp (Go's zero time and an absent
    value are not: they mean never and unknown)."""
    if value is None or value == "":
        return False
    return not isinstance(value, str) or (
        not value.startswith("0001-01-01") and parse_time(value) is None
    )


def iso(stamp):
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else None


def age_seconds(stamp, now):
    """Seconds from `stamp` to `now` (negative if the clocks disagree), None when unknown."""
    return round((now - stamp).total_seconds(), 1) if stamp else None
