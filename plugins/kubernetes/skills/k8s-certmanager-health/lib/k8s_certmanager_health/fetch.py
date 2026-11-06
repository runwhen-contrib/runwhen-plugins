"""Fetching the target Certificate (the only I/O besides k8s.py)."""

from k8s_certmanager_health.errors import RwError
from k8s_certmanager_health.k8s import kubectl_json

TYPE = "kubernetes/certificate"
PLURAL = "certificates"
# Fully qualified so a CRD from another group that also has a `certificates` plural cannot answer.
CERTIFICATES = "certificates.cert-manager.io"


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


def load_certificate(ctx):
    """The target Certificate. A missing one raises (NotFound from kubectl)."""
    if target_type(ctx) != TYPE:
        raise RwError(
            "invalid_input", f"k8s-certmanager-health does not apply to {target_type(ctx)!r}"
        )
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", CERTIFICATES, ctx.resource.name)
