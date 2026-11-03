"""Fetching the target claim and the pods that mount it (the only I/O besides k8s.py)."""

from k8s_pvc_health.errors import RwError
from k8s_pvc_health.k8s import kubectl_json, kubectl_json_scoped
from k8s_pvc_health.pvc import mounting_pods

TYPE = "kubernetes/persistentvolumeclaim"
PLURAL = "persistentvolumeclaims"


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return (
        f"kubernetes/{segments[-2].rstrip('s')}"
        if len(segments) >= 2 and segments[-2] == PLURAL
        else None
    )


def load_claim(ctx):
    """The target PersistentVolumeClaim. A missing one raises (NotFound from kubectl)."""
    if target_type(ctx) != TYPE:
        raise RwError("invalid_input", f"k8s-pvc-health does not apply to {target_type(ctx)!r}")
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "persistentvolumeclaims", ctx.resource.name
    )


def load_pods(ctx):
    """Every Pod in the claim's namespace (a Pod names its claims in spec.volumes, not by label)."""
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", "pods")["items"]


def load_mounting(ctx):
    """(claim, pods, mounting) for the target: the Pod objects and mounting_pods() of them."""
    claim = load_claim(ctx)
    pods = load_pods(ctx)
    return claim, pods, mounting_pods(pods, claim["metadata"]["name"])


def load_mounting_optional(ctx):
    """(claim, mounting) where mounting is None when the pods list is forbidden.

    For tasks whose primary facts come from the claim: a forbidden pods list is
    reported with ctx.error (permission_denied, scope pods) and the claim facts
    are kept. Any other failure raises.
    """
    claim = load_claim(ctx)
    pods, _state = kubectl_json_scoped(ctx, "pods", "-n", ctx.resource.namespace, "get", "pods")
    if pods is None:
        return claim, None
    return claim, mounting_pods(pods["items"], claim["metadata"]["name"])
