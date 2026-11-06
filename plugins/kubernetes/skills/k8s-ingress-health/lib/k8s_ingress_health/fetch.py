"""Fetching the target Ingress and the objects around it (the only I/O besides k8s.py)."""

from k8s_ingress_health.errors import RwError
from k8s_ingress_health.ingress import MAX_SERVICES
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
    """({name: Service object, or None when it does not exist}, truncated) for the backend Services.

    One list of the namespace's Services, filtered here. At most MAX_SERVICES
    distinct names are looked up (`truncated` says names were left over, and they
    are left out of the map). Forbidden is reported with ctx.error (scope
    `services`) and gives an empty map: the names are "not looked up", never
    "missing". Any other failure raises.
    """
    wanted = names[:MAX_SERVICES]
    truncated = len(names) > MAX_SERVICES
    if not wanted:
        return {}, truncated
    doc, state = kubectl_json_scoped(
        ctx, "services", "-n", ctx.resource.namespace, "get", "services"
    )
    if state == "denied":
        return {}, truncated
    by_name = {i["metadata"]["name"]: i for i in doc["items"]}
    return {n: by_name.get(n) for n in wanted}, truncated


def fetch_endpoint_slices(ctx, services):
    """{name: [EndpointSlice]} for the Services that exist, in one list by service-name label.

    Forbidden is reported with ctx.error (scope `endpointslices`) and gives an
    empty map (unknown, not "no endpoints"); a Service that exists but has no
    slices maps to [].
    """
    names = [n for n, svc in services.items() if svc is not None]
    if not names:
        return {}
    doc, state = kubectl_json_scoped(
        ctx,
        "endpointslices",
        "-n",
        ctx.resource.namespace,
        "get",
        "endpointslices.discovery.k8s.io",
        "-l",
        f"kubernetes.io/service-name in ({','.join(names)})",
    )
    if state == "denied":
        return {}
    by_service = {n: [] for n in names}
    for sl in doc["items"]:
        label = (sl["metadata"].get("labels") or {}).get("kubernetes.io/service-name")
        if label in by_service:
            by_service[label].append(sl)
    return by_service
