"""Bounds and payload errors shared by the pure functions."""

from k8s_otelcollector_health.errors import RwError

NESTED_LIMIT = 50


def cap(items, max_items):
    """Bound a top-level list output to max_items entries."""
    return list(items)[: int(max_items)]


def bad(what, why):
    """The error for a payload this skill does not understand: never read as healthy."""
    return RwError("internal", f"unexpected {what}: {why}")


def number(value):
    """Round a float for output; ints stay ints."""
    return round(value, 2) if isinstance(value, float) else value
