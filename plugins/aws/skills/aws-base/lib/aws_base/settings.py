"""Settings the aws-base scripts read, validated before any call."""

from types import SimpleNamespace

from .awscli import check_region
from .errors import RwError


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input."""
    max_items = settings.max_items
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10000:
        raise RwError("invalid_input", "setting maxItems must be an integer from 1 to 10000")
    regions = list(settings.regions or [])
    if len(regions) > 40:
        raise RwError("invalid_input", "setting regions names at most 40 regions")
    for region in regions:
        check_region(region)
    return SimpleNamespace(max_items=max_items, regions=list(dict.fromkeys(regions)))
