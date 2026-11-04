"""What every azure-kv-health task shares: the target, the settings, the time window, text caps
and the one `az keyvault show` that turns a vault name into its ARM resource id."""

import re
from datetime import timedelta
from types import SimpleNamespace

from .az import az_json, command_timeout, parse_duration
from .errors import RwError

TYPE = "azure/keyvault"
TEXT_LIMIT = 1024  # every free-text field (messages, summaries) is cut to this many characters
NESTED_LIMIT = 50  # nested lists (datapoints, settings) are cut to this many entries
MIN_WINDOW = 60.0  # seconds
MAX_WINDOW = 30 * 86400.0  # Azure Monitor keeps metrics 93 days; a month is more than a task needs
INTERVALS = ("PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H", "P1D")
_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def target(resource):
    """(subscription id, vault name) from the run's target; anything but an azure/keyvault is
    refused. The subscription is an identity field of the target (its parent in the path)."""
    resource = resource or {}
    if resource.get("type") != TYPE or not resource.get("name"):
        raise RwError(
            "invalid_input", f"this task runs on an azure/keyvault, got {resource.get('type')!r}"
        )
    subscription = str(resource.get("subscription") or "")
    if not _GUID.match(subscription):
        raise RwError("invalid_input", "the target has no subscription id")
    return subscription.lower(), str(resource["name"])


def clip(text, limit=TEXT_LIMIT):
    """(text cut to `limit` characters, whether it was cut). None and non-strings become ''."""
    text = text if isinstance(text, str) else ""
    return (text, False) if len(text) <= limit else (text[: limit - 3] + "...", True)


def number(settings, name, snake, low, high, integer=False):
    value = getattr(settings, snake)
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise RwError("invalid_input", f"setting {name} must be a number")
    if not low <= value <= high:
        raise RwError("invalid_input", f"setting {name} must be from {low} to {high}")
    return value


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input."""
    max_items = number(settings, "maxItems", "max_items", 1, 10000, integer=True)
    command_timeout(settings)  # commandTimeout: a duration, checked here
    interval = getattr(settings, "metric_interval", "PT1H")
    if interval not in INTERVALS:
        raise RwError(
            "invalid_input", f"setting metricInterval must be one of {', '.join(INTERVALS)}"
        )
    return SimpleNamespace(
        max_items=max_items,
        availability_min_percent=number(
            settings, "availabilityMinPercent", "availability_min_percent", 0, 100
        ),
        request_threshold=number(settings, "requestThreshold", "request_threshold", 0, 10**9),
        latency_threshold_ms=number(
            settings, "latencyThresholdMs", "latency_threshold_ms", 0, 10**7
        ),
        metric_interval=interval,
    )


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
    cut, total, omitted = bound(items, limit)
    return {"items": cut, "total": total, "omitted": omitted}


def resolve(ctx):
    """The vault the task targets, read through `az keyvault show`: (subscription, name, ARM id,
    document). A vault that is gone is not_found; the id is what the Monitor commands take."""
    subscription, name = target(ctx.resource)
    doc = az_json(ctx, ["keyvault", "show", "--name", name, "--subscription", subscription])
    if not isinstance(doc, dict) or not doc.get("id"):
        raise RwError("internal", "az keyvault show returned no vault")
    if str(doc.get("name", "")).lower() != name.lower():
        raise RwError("internal", f"az keyvault show answered for another vault than {name}")
    return subscription, name, str(doc["id"]), doc
