"""Settings the gcp-bucket-health scripts read, validated before any gcloud call."""

from types import SimpleNamespace

from .errors import RwError
from .gcloud import command_timeout, parse_duration


def _integer(settings, name, low, high):
    value = getattr(settings, name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        camel = name.split("_")[0] + "".join(p.title() for p in name.split("_")[1:])
        raise RwError("invalid_input", f"setting {camel} must be an integer from {low} to {high}")
    return value


def read_settings(settings, *names):
    """The task's settings as a namespace; a bad value raises RwError invalid_input. `names` are
    the optional ones this task declares: usage_threshold_tib, public_bucket_threshold,
    size_timeout."""
    out = {
        "max_items": _integer(settings, "max_items", 1, 10000),
        "command_timeout": command_timeout(settings),
    }
    if "usage_threshold_tib" in names:
        value = settings.usage_threshold_tib
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1e6:
            raise RwError(
                "invalid_input", "setting usageThresholdTib must be a number from 0 to 1000000"
            )
        out["usage_threshold_tib"] = float(value)
    if "public_bucket_threshold" in names:
        out["public_bucket_threshold"] = _integer(settings, "public_bucket_threshold", 0, 10000)
    if "size_timeout" in names:
        out["size_timeout"] = parse_duration(settings.size_timeout, "sizeTimeout")
    return SimpleNamespace(**out)
