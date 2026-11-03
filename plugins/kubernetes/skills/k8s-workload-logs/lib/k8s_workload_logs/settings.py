"""Settings the scripts read, validated before any kubectl call."""

from types import SimpleNamespace

from k8s_workload_logs.errors import RwError

# name -> (minimum, maximum); enforced here because the platform may not enforce tasks.yaml min/max
INTEGERS = {
    "max_items": (1, 10000),
    "max_pods": (1, 500),
    "max_lines": (1, 100000),
    "max_scan_lines": (1, 100000),
    "max_bytes_total": (1 << 20, 1 << 30),
    "scan_budget_seconds": (1, 600),
}


def read_settings(settings):
    """The task's settings as a namespace; a value out of range raises RwError invalid_input."""
    out = {}
    for name, (low, high) in INTEGERS.items():
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise RwError(
                "invalid_input",
                f"setting {name} must be an integer from {low} to {high}, got {value!r}",
            )
        out[name] = value
    exclude = getattr(settings, "exclude_containers")
    if not isinstance(exclude, (list, tuple)) or not all(isinstance(x, str) for x in exclude):
        raise RwError(
            "invalid_input", "setting exclude_containers must be a list of container names"
        )
    out["exclude_containers"] = list(exclude)
    out["patterns"] = getattr(settings, "patterns")
    out["ignore"] = getattr(settings, "ignore")
    return SimpleNamespace(**out)
