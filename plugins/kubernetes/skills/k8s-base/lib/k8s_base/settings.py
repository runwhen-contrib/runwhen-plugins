"""Settings the kubectl script reads, validated before any kubectl call."""

import re
from types import SimpleNamespace

from k8s_base.errors import RwError

MAX_OUTPUT_LINES = (1, 100000)


def _strings(settings, name):
    value = getattr(settings, name)
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise RwError("invalid_input", f"setting {name} must be a list of strings")
    return list(value)


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input."""
    lines = settings.max_output_lines
    low, high = MAX_OUTPUT_LINES
    if isinstance(lines, bool) or not isinstance(lines, int) or not low <= lines <= high:
        raise RwError(
            "invalid_input",
            f"setting maxOutputLines must be an integer from {low} to {high}, got {lines!r}",
        )
    return SimpleNamespace(
        allowed_verbs=_strings(settings, "allowed_verbs"),
        denied_resources=_strings(settings, "denied_resources"),
        allowed_flags=_strings(settings, "allowed_flags"),
        max_output_lines=lines,
    )


_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)")
_UNIT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text, name="duration"):
    """Seconds of a duration like 480s or 1m30s (ms|s|m|h|d, combinable); anything else raises
    RwError invalid_input."""
    if isinstance(text, str) and re.fullmatch(r"(?:\d+(?:ms|s|m|h|d))+", text):
        return sum(int(n) * _UNIT[u] for n, u in _DURATION.findall(text))
    raise RwError("invalid_input", f"setting {name} must be a duration like 480s, got {text!r}")
