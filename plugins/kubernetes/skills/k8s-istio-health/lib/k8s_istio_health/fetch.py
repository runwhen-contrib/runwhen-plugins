"""Fetching the target and the objects around it (the only I/O besides k8s.py)."""

from k8s_istio_health.common import deployment_pods, selector_string
from k8s_istio_health.errors import RwError
from k8s_istio_health.k8s import kubectl_json, kubectl_json_scoped

NAMESPACE = "kubernetes/namespace"
DEPLOYMENT = "kubernetes/deployment"


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment before the name at the
    end of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    parts = (getattr(ctx.resource, "path", None) or "").strip("/").split("/")
    plural = parts[-2] if len(parts) >= 2 else ""
    return f"kubernetes/{plural[:-1]}" if plural.endswith("s") else None


def require_type(ctx, wanted):
    """Refuse any other target type before a kubectl call is made."""
    found = target_type(ctx)
    if found != wanted:
        raise RwError(
            "invalid_input",
            f"this task applies to {wanted}, not {found!r}",
        )


def load_namespace(ctx):
    """The target Namespace object, or None when the identity may not read Namespaces
    (cluster-scoped; recorded with ctx.error, scope `namespaces`). A deleted namespace raises
    not_found, never an empty result."""
    require_type(ctx, NAMESPACE)
    return kubectl_json_scoped(
        ctx,
        "namespaces",
        "get",
        "namespaces",
        ctx.resource.name,
        hint="grant get on namespaces through a ClusterRole to read the injection labels",
    )


def load_deployment(ctx):
    """The target Deployment. A missing one raises (NotFound from kubectl)."""
    require_type(ctx, DEPLOYMENT)
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "deployments.apps", ctx.resource.name
    )


def target_namespace(ctx):
    """The namespace to list in: the target's own name for a Namespace, else its namespace."""
    if target_type(ctx) == NAMESPACE:
        return ctx.resource.name
    return ctx.resource.namespace


def list_items(ctx, *args):
    """A list the task cannot do without: a failure raises."""
    return kubectl_json(ctx, "-n", target_namespace(ctx), "get", *args)["items"]


def list_scoped(ctx, scope, *args):
    """A list that only enriches the result: None, with a permission_denied ctx.error scoped
    to `scope`, when it is forbidden."""
    doc = kubectl_json_scoped(ctx, scope, "-n", target_namespace(ctx), "get", *args)
    return None if doc is None else doc["items"]


def owned_pods(ctx, deployment):
    """The Deployment's pods (selector + owner chain). A failed list raises."""
    selector = selector_string(deployment["spec"]["selector"])
    replicasets = list_items(ctx, "replicasets.apps", "-l", selector)
    pods = list_items(ctx, "pods", "-l", selector)
    return deployment_pods(deployment, replicasets, pods)
