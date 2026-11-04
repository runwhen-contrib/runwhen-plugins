"""Settings the gcp-base scripts read, validated before any gcloud call."""

from types import SimpleNamespace

from .errors import RwError
from .gcloud import command_timeout


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input."""
    max_items = settings.max_items
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10000:
        raise RwError("invalid_input", "setting maxItems must be an integer from 1 to 10000")
    return SimpleNamespace(max_items=max_items, command_timeout=command_timeout(settings))
