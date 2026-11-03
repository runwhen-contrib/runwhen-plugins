"""Fetching the target's nodes, pods, events and metrics (the only I/O besides k8s.py)."""

from k8s_node_health.errors import RwError
from k8s_node_health.k8s import kubectl_json, kubectl_json_scoped

TYPES = ("kubernetes/cluster", "kubernetes/node")
_PLURALS = {"clusters": "kubernetes/cluster", "nodes": "kubernetes/node"}
EVENTS_NAMESPACE = "default"  # where the control plane and kubelets write Node events


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return _PLURALS.get(segments[-2]) if len(segments) >= 2 else None


def node_name(ctx):
    """The node name for a node target, None for a cluster target; anything else is invalid_input."""
    rtype = target_type(ctx)
    if rtype not in TYPES:
        raise RwError("invalid_input", f"k8s-node-health does not apply to {rtype!r}")
    return ctx.resource.name if rtype == "kubernetes/node" else None


def load_nodes(ctx):
    """The target node, or every node for a cluster target. A missing node raises (NotFound from kubectl)."""
    name = node_name(ctx)
    if name:
        return [kubectl_json(ctx, "get", "nodes", name)]
    return kubectl_json(ctx, "get", "nodes")["items"]


def load_pods(ctx):
    """Pods in every namespace, only those bound to the node for a node target."""
    name = node_name(ctx)
    args = ["get", "pods", "-A"]
    if name:
        args += ["--field-selector", f"spec.nodeName={name}"]
    return kubectl_json(ctx, *args)["items"]


def load_node_events(ctx):
    """Node events from the default namespace, or None when they are forbidden (reported with ctx.error)."""
    name = node_name(ctx)
    selector = "involvedObject.kind=Node" + (f",involvedObject.name={name}" if name else "")
    doc, _ok = kubectl_json_scoped(
        ctx,
        "events",
        f"grant list on events in the {EVENTS_NAMESPACE} namespace through a Role or ClusterRole",
        "-n",
        EVENTS_NAMESPACE,
        "get",
        "events",
        "--field-selector",
        selector,
    )
    return None if doc is None else doc["items"]


def load_node_metrics(ctx):
    """NodeMetrics objects. Raises RwError(dependency) when metrics.k8s.io is not served."""
    try:
        return kubectl_json(ctx, "get", "nodes.metrics.k8s.io")["items"]
    except RwError as exc:
        if exc.code == "dependency":
            raise RwError("dependency", "metrics.k8s.io not available") from None
        raise
