"""Settings the pg-connections script reads, validated before any query."""

from types import SimpleNamespace

from .errors import RwError
from .pg import timeouts


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input."""
    max_items = settings.max_items
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10000:
        raise RwError("invalid_input", "setting maxItems must be an integer from 1 to 10000")
    timeouts(settings)  # connectTimeout and statementTimeout: durations, checked here
    return SimpleNamespace(max_items=max_items)
