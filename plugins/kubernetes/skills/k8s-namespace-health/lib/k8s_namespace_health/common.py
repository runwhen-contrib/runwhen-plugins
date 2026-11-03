"""Time, selector and quantity helpers shared by the k8s-namespace-health modules.

Pure functions over already-parsed Kubernetes JSON: no kubectl, no clock.
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

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


def within(dt, since, now):
    return dt is not None and dt >= now - parse_duration(since)


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


def matches_selector(labels, selector):
    """Whether `labels` satisfy a LabelSelector (matchLabels + matchExpressions).

    An empty selector `{}` matches everything, as it does in a PodDisruptionBudget.
    """
    labels = labels or {}
    selector = selector or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
        if op not in ("In", "NotIn", "Exists", "DoesNotExist"):
            raise ValueError(f"unknown selector operator {op!r}")
    return True


_QUANTITY = re.compile(
    r"^([+-]?(?:\d+\.?\d*|\.\d+))(?:([eE][+-]?\d+)|(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E))?$"
)
_SUFFIX = {
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 1000),
    "k": Fraction(1000),
    "M": Fraction(1000**2),
    "G": Fraction(1000**3),
    "T": Fraction(1000**4),
    "P": Fraction(1000**5),
    "E": Fraction(1000**6),
    "Ki": Fraction(1024),
    "Mi": Fraction(1024**2),
    "Gi": Fraction(1024**3),
    "Ti": Fraction(1024**4),
    "Pi": Fraction(1024**5),
    "Ei": Fraction(1024**6),
}


def parse_quantity(text):
    """Kubernetes resource quantity ('500m', '1Gi', '2', '1e3') -> Fraction.

    An unknown suffix raises instead of being read as a plain number.
    """
    m = _QUANTITY.match(str(text).strip())
    if not m:
        raise ValueError(f"not a resource quantity: {text!r}")
    number, exponent, suffix = m.groups()
    value = Fraction(Decimal(number))
    if exponent:
        value *= Fraction(10) ** int(exponent[1:])
    elif suffix:
        value *= _SUFFIX[suffix]
    return value
