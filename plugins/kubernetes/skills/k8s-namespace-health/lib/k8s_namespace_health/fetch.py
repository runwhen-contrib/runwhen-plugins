"""Fetching the target namespace and the objects the tasks look at (the only I/O besides k8s.py)."""

from k8s_namespace_health.common import parse_duration
from k8s_namespace_health.k8s import RwError, kubectl_json, secondary

NAMESPACE_TYPE = "kubernetes/namespace"


def target_type(ctx):
    """Qualified type of the target: `ctx.resource.type`, else derived from the
    plural segment before the name at the end of `ctx.resource.path`."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    parts = (getattr(ctx.resource, "path", None) or "").split("/")
    if len(parts) >= 2 and parts[-2] == "namespaces":
        return NAMESPACE_TYPE
    return None


def check_duration(value):
    """Reject a bad duration before any kubectl call."""
    try:
        parse_duration(value)
    except ValueError as exc:
        raise RwError("invalid_input", str(exc)) from exc


# Bounds of the numeric settings (the same min/max tasks.yaml declares), enforced here
# too so an out-of-range value fails as invalid_input before any kubectl call.
SETTING_BOUNDS = {
    "max_items": (1, None),
    "restart_threshold": (1, None),
    "job_active_max_minutes": (1, None),
    "quota_ratio": (0, 1),
}


def check_settings(ctx, *names):
    for name in names:
        value = getattr(ctx.settings, name)
        low, high = SETTING_BOUNDS[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < low
            or (high is not None and value > high)
        ):
            raise RwError(
                "invalid_input",
                f"setting {name} must be between {low} and {high or 'any'}, got {value!r}",
            )


def load_namespace(ctx):
    """The namespace the tasks look in: the target's name.

    The Namespace object is read first so that a deleted namespace raises
    `not_found` instead of listing as empty. An identity that may not read
    Namespaces (namespace-scoped RBAC) is not stopped by that: the denial is
    recorded with `ctx.error` (scope `namespaces`, making the result partial) and
    the lists inside the namespace are tried; if they are forbidden too, they raise.
    """
    rtype = target_type(ctx)
    if rtype != NAMESPACE_TYPE:
        raise RwError("invalid_input", f"k8s-namespace-health does not apply to {rtype!r}")
    secondary(ctx, "namespaces", "get", "namespaces", ctx.resource.name, code_raises=("not_found",))
    return ctx.resource.name


def list_items(ctx, namespace, resource, *args):
    """A list the task cannot do without: a failure raises."""
    return kubectl_json(ctx, "-n", namespace, "get", resource, *args)["items"]


def list_secondary(ctx, namespace, resource, *args):
    """A list that only enriches the result (owners, coverage): a failure is
    recorded with `ctx.error` scoped to the resource and None is returned."""
    doc = secondary(ctx, resource, "-n", namespace, "get", resource, *args)
    return None if doc is None else doc["items"]
