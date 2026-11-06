"""Time, bounds and quantity helpers shared by the k8s-node-health functions.

Pure: parsed values in, plain values out. No kubectl, no clock.
"""

import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from k8s_node_health.errors import RwError

NESTED_LIMIT = 50  # most entries in a list nested inside an output entry
TEXT_LIMIT = 1024  # most characters in a free-text field

# ---------------------------------------------------------------- time ----

_DURATION = re.compile(r"(\d+)\s*(ms|[smhd])")
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta. Anything else raises RwError(invalid_input)."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise RwError("invalid_input", f"not a duration: {text!r}")
    return timedelta(milliseconds=sum(int(n) * _UNIT_MS[u] for n, u in parts))


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


def seconds_since(dt, now):
    return max(0, int((now - dt).total_seconds())) if dt else None


# -------------------------------------------------------------- bounds ----


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


def nested(items):
    """Bound a list nested inside an output entry to NESTED_LIMIT entries."""
    return list(items)[:NESTED_LIMIT]


def trunc(text):
    """(text cut to TEXT_LIMIT characters, whether it was cut). None stays None, not cut."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:TEXT_LIMIT], True) if len(text) > TEXT_LIMIT else (text, False)


# ------------------------------------------------------------ quantity ----

_QUANTITY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:([eE][+-]?\d+)|([A-Za-z]{0,2}))$")
_SUFFIX = {
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 10**3),
    "": Fraction(1),
    "k": Fraction(10**3),
    "K": Fraction(10**3),
    "M": Fraction(10**6),
    "G": Fraction(10**9),
    "T": Fraction(10**12),
    "P": Fraction(10**15),
    "E": Fraction(10**18),
    "Ki": Fraction(2**10),
    "Mi": Fraction(2**20),
    "Gi": Fraction(2**30),
    "Ti": Fraction(2**40),
    "Pi": Fraction(2**50),
    "Ei": Fraction(2**60),
}


def parse_quantity(text):
    """A Kubernetes quantity ('8', '500m', '123456789n', '4012892Ki', '1.5Gi', '129e6') -> Fraction, or None for no value.

    The result is in base units (cores, bytes), exact. Every suffix is
    understood; a bare number is NOT millicores.
    """
    if text is None or text == "":
        return None
    m = _QUANTITY.match(str(text).strip())
    if not m:
        raise ValueError(f"not a quantity: {text!r}")
    number, exp, suffix = m.groups()
    if exp is not None:
        return Fraction(number) * Fraction(10) ** int(exp[1:])
    if suffix not in _SUFFIX:
        raise ValueError(f"not a quantity: {text!r}")
    return Fraction(number) * _SUFFIX[suffix]


def number(value):
    """A Fraction as an int when whole, else a float to 3 places."""
    return int(value) if value.denominator == 1 else round(float(value), 3)


def cpu_millicores(text):
    """CPU quantity -> millicores (int when whole, else a float to 3 places), or None.

    '8' is 8000, '500m' is 500, '1.5' is 1500, '250000000n' is 250.
    """
    q = parse_quantity(text)
    return None if q is None else number(q * 1000)


def memory_bytes(text):
    """Memory quantity -> bytes (int), or None."""
    q = parse_quantity(text)
    return None if q is None else int(round(q))


def percent(used, total):
    """used / total as a percentage to one decimal, or None when total is missing or zero."""
    if used is None or not total:
        return None
    return round(used / total * 100, 1)
