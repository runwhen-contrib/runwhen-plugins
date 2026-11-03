"""Bounds and payload checks shared by the pure functions."""

from k8s_seaweedfs_health.errors import RwError

TEXT_LIMIT = 1024
NESTED_LIMIT = 50


def limit_text(value, limit=TEXT_LIMIT):
    """(text, truncated) for a free-text field."""
    if value is None:
        return None, False
    value = str(value)
    return value[:limit], len(value) > limit


def cap(items, max_items):
    """Bound a top-level list output to max_items entries."""
    return list(items)[: int(max_items)]


def nested(items):
    """A nested list cut at NESTED_LIMIT."""
    return list(items)[:NESTED_LIMIT]


def bad(what, why):
    """The error for a payload this skill does not understand: never read as healthy."""
    return RwError("internal", f"unexpected {what}: {why}")


def need(doc, key, kinds, what):
    """doc[key], which must be present and of one of `kinds` (a bool is never a number)."""
    kinds = kinds if isinstance(kinds, tuple) else (kinds,)
    if not isinstance(doc, dict) or key not in doc:
        raise bad(what, f"missing {key!r}")
    value = doc[key]
    if isinstance(value, bool) and bool not in kinds:
        raise bad(what, f"{key!r} is a boolean")
    if not isinstance(value, kinds):
        raise bad(what, f"{key!r} is {type(value).__name__}")
    return value


def maybe(doc, key, kinds, what):
    """doc[key] when present and not null (then of one of `kinds`), else None."""
    if not isinstance(doc, dict) or doc.get(key) is None:
        return None
    return need(doc, key, kinds, what)


def number(value):
    """Round a float for output; ints stay ints."""
    return round(value, 2) if isinstance(value, float) else value
