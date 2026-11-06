"""Fetching the target Application (the only I/O besides k8s.py)."""

from k8s_argocd_health.errors import RwError
from k8s_argocd_health.k8s import kubectl_json

TYPE = "kubernetes/application"
PLURAL = "applications"
# The fully qualified resource name: other API groups also define an Application kind.
RESOURCE = "applications.argoproj.io"


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


def load_application(ctx):
    """The target Application. A missing one raises (NotFound from kubectl)."""
    if target_type(ctx) != TYPE:
        raise RwError("invalid_input", f"k8s-argocd-health does not apply to {target_type(ctx)!r}")
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", RESOURCE, ctx.resource.name)
