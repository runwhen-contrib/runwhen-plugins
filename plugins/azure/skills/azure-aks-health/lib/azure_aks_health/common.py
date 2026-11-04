"""What every azure skill shares besides running az: the caps on what a task returns, the settings
every task declares, and the look-back window.

Like `az.py`, this file is copied verbatim into each skill's `lib/<skill_snake>/` (a test keeps the
copies equal), so it imports only its siblings. What belongs to one skill, such as how its target
is read from `ctx.resource`, lives in that skill's own modules.

    from <skill_snake>.common import clip, listing, read_settings, window, span
"""

import re
from datetime import timedelta
from types import SimpleNamespace

from .az import command_timeout, parse_duration
from .errors import RwError

TEXT_LIMIT = 1024  # every free-text field (messages, summaries) is cut to this many characters
NESTED_LIMIT = 50  # nested lists (datapoints, settings) are cut to this many entries
MIN_WINDOW = 60.0  # seconds
MAX_WINDOW = 30 * 86400.0  # Azure Monitor keeps metrics 93 days; a month is more than a task needs
INTERVALS = ("PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H", "P1D")
GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def clip(text, limit=TEXT_LIMIT):
    """(text cut to `limit` characters, whether it was cut). None and non-strings become ''."""
    text = text if isinstance(text, str) else ""
    return (text, False) if len(text) <= limit else (text[: limit - 3] + "...", True)


def number(settings, name, snake, low, high, integer=False):
    """The setting `name` (read as `snake` from the settings object) as a number in [low, high];
    anything else raises invalid_input."""
    value = getattr(settings, snake)
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise RwError("invalid_input", f"setting {name} must be a number")
    if not low <= value <= high:
        raise RwError("invalid_input", f"setting {name} must be from {low} to {high}")
    return value


def read_settings(settings):
    """The settings every azure task declares, checked before any az call: maxItems, the
    commandTimeout duration and, for a task that reads metrics, metricInterval. A bad value raises
    RwError invalid_input. A skill's own settings (thresholds) are read by the skill."""
    max_items = number(settings, "maxItems", "max_items", 1, 10000, integer=True)
    command_timeout(settings)  # commandTimeout: a duration, checked here
    interval = getattr(settings, "metric_interval", "PT1H")
    if interval not in INTERVALS:
        raise RwError(
            "invalid_input", f"setting metricInterval must be one of {', '.join(INTERVALS)}"
        )
    return SimpleNamespace(max_items=max_items, metric_interval=interval)


def window(value, name="window"):
    """Seconds of a look-back input such as `24h`, within the range Azure Monitor serves."""
    seconds = parse_duration(value, name)
    if not MIN_WINDOW <= seconds <= MAX_WINDOW:
        raise RwError("invalid_input", f"{name} must be from 1m to 30d, got {value!r}")
    return seconds


def timestamp(moment):
    """RFC 3339 UTC with a Z, whole seconds: what az takes for --start-time and --end-time."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def span(now, seconds):
    """(start, end) timestamps of the look-back window that ends at `now`."""
    return timestamp(now - timedelta(seconds=seconds)), timestamp(now)


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def listing(items, limit):
    """{items: first `limit`, total, omitted}: a list output cut to maxItems, with what was cut."""
    cut, total, omitted = bound(items, limit)
    return {"items": cut, "total": total, "omitted": omitted}
