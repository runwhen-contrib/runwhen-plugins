"""Fetching the target claim and the pods that mount it (the only I/O besides k8s.py)."""

from k8s_pvc_health.errors import RwError
from k8s_pvc_health.k8s import kubectl_json
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
