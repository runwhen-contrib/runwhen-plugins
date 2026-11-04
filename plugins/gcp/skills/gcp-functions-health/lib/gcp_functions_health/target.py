"""The function a task runs on, and the settings and inputs it reads, validated before any call."""

from types import SimpleNamespace
from typing import NamedTuple

from .errors import RwError
from .fnlist import valid_function_id, valid_region
from .gcloud import command_timeout, parse_duration, valid_project

TYPE = "gcp/cloud-function"
TASK_BUDGET = 180  # seconds: every task's `timeout` in tasks.yaml, 3x the default commandTimeout
RESERVE = 10.0  # seconds kept free after the last call for the result
MAX_SINCE_SECONDS = 14 * 86400  # Cloud Monitoring keeps most metrics 6 weeks; logs 30 days


class Target(NamedTuple):
    project: str
    name: str
    region: str | None  # the region ancestor of the target's path; None: found by listing


def function_target(resource):
    """The function a run targets as Target(project, name, region). A function's path is
    `gcp/projects/<project>/regions/<region>/cloud-functions/<name>`: its region is the chain's
    (the `region` ancestor), never an input. A path with no region ancestor
    (`gcp/projects/<project>/cloud-functions/<name>`) is accepted, and then the function is found
    by listing the project (locate.py). Anything but a gcp/cloud-function is refused."""
    resource = resource or {}
    parts = str(resource.get("path", "")).split("/")
    if resource.get("type") != TYPE or parts[:2] != ["gcp", "projects"]:
        raise RwError("invalid_input", f"this task runs on a {TYPE}, got {resource.get('type')!r}")
    if len(parts) == 7 and parts[3] == "regions" and parts[5] == "cloud-functions":
        project, region, name = parts[2], parts[4], parts[6]
    elif len(parts) == 5 and parts[3] == "cloud-functions":
        project, region, name = parts[2], None, parts[4]
    else:
        raise RwError("invalid_input", f"not the path of a {TYPE}: {resource.get('path')!r}")
    if not valid_project(project):
        raise RwError("invalid_input", "the target's project is not a valid project id")
    if not valid_function_id(name):
        raise RwError("invalid_input", f"not a function id: {name[:64]!r}")
    if region is not None and not valid_region(region):
        raise RwError("invalid_input", f"not a region name: {str(region)[:64]!r}")
    return Target(project, name, region)


def _integer(settings, name, camel, low, high, default):
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RwError("invalid_input", f"setting {camel} must be an integer from {low} to {high}")
    return value


def _ratio(settings, name, camel, low, high, default):
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise RwError("invalid_input", f"setting {camel} must be a number from {low} to {high}")
    return float(value)


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input. Settings a
    task does not declare read as their defaults."""
    max_items = _integer(settings, "max_items", "maxItems", 1, 10000, 200)
    max_entries = _integer(settings, "max_log_entries", "maxLogEntries", 1, 1000, 50)
    return SimpleNamespace(
        max_items=max_items,
        max_log_entries=min(max_entries, max_items),
        command_timeout=command_timeout(settings),
        error_rate_threshold=_ratio(
            settings, "error_rate_threshold", "errorRateThreshold", 0, 1, 0.05
        ),
        latency_timeout_ratio=_ratio(
            settings, "latency_timeout_ratio", "latencyTimeoutRatio", 0.1, 1, 0.9
        ),
    )


def read_since(value):
    """The look-back in seconds from the `since` input (a duration such as 1h or 90m)."""
    seconds = parse_duration(value, "since")
    if seconds < 60:
        raise RwError(
            "invalid_input", "since must be at least 60s (Cloud Monitoring's finest period)"
        )
    if seconds > MAX_SINCE_SECONDS:
        raise RwError("invalid_input", "since must be at most 14d")
    return int(seconds)
