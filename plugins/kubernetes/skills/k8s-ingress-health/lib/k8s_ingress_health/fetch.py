"""Fetching the target Ingress and the objects around it (the only I/O besides k8s.py)."""

from k8s_ingress_health.errors import RwError
from k8s_ingress_health.ingress import endpoint_counts
from k8s_ingress_health.k8s import kubectl_json, kubectl_json_scoped

TYPE = "kubernetes/ingress"
PLURAL = "ingresses"


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return TYPE if len(segments) >= 2 and segments[-2] == PLURAL else None


def load_ingress(ctx):
    """The target Ingress. A missing one raises (NotFound from kubectl)."""
    if target_type(ctx) != TYPE:
        raise RwError("invalid_input", f"k8s-ingress-health does not apply to {target_type(ctx)!r}")
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "ingresses.networking.k8s.io", ctx.resource.name
    )


def fetch_services(ctx, names):
    """{name: Service object, or None when it does not exist} for the backend Services.

    Forbidden is reported once with ctx.error (scope `services`) and the read
    stops: the names not reached are left out of the map, which callers read as
    "not looked up", never as missing. Any other failure raises.
    """
    found = {}
    for name in names:
        svc, state = kubectl_json_scoped(
            ctx, "services", "-n", ctx.resource.namespace, "get", "service", name, missing_ok=True
        )
        if state == "denied":
            break
        found[name] = svc
    return found


def fetch_endpoints(ctx, services):
    """{name: endpoint_counts()} for the Services that exist, from their EndpointSlices.

    Forbidden is reported once with ctx.error (scope `endpointslices`) and the
    names not reached are left out of the map (unknown, not zero).
    """
    counts = {}
    for name, svc in services.items():
        if svc is None:
            continue
        doc, state = kubectl_json_scoped(
            ctx,
            "endpointslices",
            "-n",
            ctx.resource.namespace,
            "get",
            "endpointslices.discovery.k8s.io",
            "-l",
            f"kubernetes.io/service-name={name}",
        )
        if state == "denied":
            break
        counts[name] = endpoint_counts(doc["items"])
    return counts
