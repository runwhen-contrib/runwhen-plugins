"""Settings the azure-base scripts read, validated before any az call."""

from types import SimpleNamespace

from .az import command_timeout
from .errors import RwError


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input."""
    max_items = settings.max_items
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10000:
        raise RwError("invalid_input", "setting maxItems must be an integer from 1 to 10000")
    command_timeout(settings)  # commandTimeout: a duration, checked here
    return SimpleNamespace(max_items=max_items)
