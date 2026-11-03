"""Fetching the target namespace and the objects the tasks look at (the only I/O besides k8s.py)."""

from k8s_namespace_resources.k8s import RwError, kubectl_json

NAMESPACE_TYPE = "kubernetes/namespace"

# The numeric settings' bounds (the same min/max tasks.yaml declares), enforced here too
# so an out-of-range value fails as invalid_input before any kubectl call.
SETTING_BOUNDS = {
    "max_items": (1, None),
    "utilization_percent": (0, None),
    "vpa_difference_percent": (0, None),
}


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


def read(ctx, *args):
    """A list or object the task can degrade without: (document, None), or
    (None, the RwError) when it was forbidden. Every other failure (missing
    API, unreachable, timeout, not found) raises."""
    try:
        return kubectl_json(ctx, *args), None
    except RwError as exc:
        if exc.code == "permission_denied":
            return None, exc
        raise


def report_denied(ctx, scope, exc):
    """Record a forbidden read on the result (making it partial)."""
    ctx.error(
        "permission_denied",
        f"not allowed to read {scope}: {exc.message}"[:1024],
        scope=scope,
        hint=f"grant get/list on {scope} through a Role in the namespace",
    )


def load_namespace(ctx, task):
    """The namespace the tasks look in: the target's name.

    The Namespace object is read first so that a deleted namespace raises
    `not_found` instead of listing as empty. An identity that may not read
    Namespaces (namespace-scoped RBAC) is not stopped by that: the denial is
    recorded with `ctx.error` (scope `namespaces`, making the result partial) and
    the lists inside the namespace are tried; if they are forbidden too, they raise.
    """
    rtype = target_type(ctx)
    if rtype != NAMESPACE_TYPE:
        raise RwError("invalid_input", f"{task} does not apply to {rtype!r}")
    _, denied = read(ctx, "get", "namespaces", ctx.resource.name)
    if denied is not None:
        report_denied(ctx, "namespaces", denied)
    return ctx.resource.name


def list_items(ctx, namespace, resource, *args):
    """A list the task cannot do without: any failure raises."""
    return kubectl_json(ctx, "-n", namespace, "get", resource, *args)["items"]


def list_optional(ctx, namespace, scope, resource, *args):
    """A list that only enriches the result (owners, current requests): when it is
    forbidden the denial is recorded and None is returned; other failures raise."""
    doc, denied = read(ctx, "-n", namespace, "get", resource, *args)
    if denied is not None:
        report_denied(ctx, scope, denied)
        return None
    return doc["items"]


def list_degradable(ctx, namespace, resources):
    """Lists the task can partly do without: {name: items or None}, name being the
    first segment of the kubectl resource.

    A forbidden list is None and is recorded with `ctx.error` (the result becomes
    partial) as long as at least one other list was read; if every list was
    forbidden there is nothing to report and the first denial raises. Every other
    failure raises at once.
    """
    docs, denied = {}, {}
    for resource in resources:
        scope = resource.split(".")[0]
        doc, exc = read(ctx, "-n", namespace, "get", resource)
        docs[scope] = None if doc is None else doc["items"]
        if exc is not None:
            denied[scope] = exc
    if denied and len(denied) == len(docs):
        raise next(iter(denied.values()))
    for scope, exc in denied.items():
        report_denied(ctx, scope, exc)
    return docs
