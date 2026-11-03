"""Fetching the target NodePool and the objects around it (the only I/O besides k8s.py)."""

from k8s_karpenter_health.errors import RwError
from k8s_karpenter_health.k8s import kubectl_json, kubectl_json_scoped

TYPE = "kubernetes/nodepool"
PLURAL = "nodepools"
# Fully qualified so a CRD of another group that also has a `nodepools` plural cannot answer.
NODEPOOLS = "nodepools.karpenter.sh"
NODECLAIMS = "nodeclaims.karpenter.sh"
POOL_LABEL = "karpenter.sh/nodepool"
# Event reasons that explain a pod waiting for capacity (scheduler and Karpenter).
EVENT_REASONS = ("FailedScheduling", "Nominated", "FailedProvisioning")


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


def load_pool(ctx):
    """The target NodePool. A missing one raises (NotFound from kubectl)."""
    if target_type(ctx) != TYPE:
        raise RwError(
            "invalid_input", f"k8s-karpenter-health does not apply to {target_type(ctx)!r}"
        )
    return kubectl_json(ctx, "get", NODEPOOLS, ctx.resource.name)


def load_nodeclass(ctx, plural_group, name):
    """(NodeClass object or None, state) for a looked-up NodeClass; see kubectl_json_scoped."""
    return kubectl_json_scoped(
        ctx,
        "nodeclasses",
        f"grant get on {plural_group} through a ClusterRole",
        "get",
        plural_group,
        name,
        missing_ok=True,
        absent_ok=True,
    )


def load_nodeclaims(ctx):
    """Every NodeClaim carrying the target pool's label (NodeClaims are cluster-scoped)."""
    return kubectl_json(ctx, "get", NODECLAIMS, "-l", f"{POOL_LABEL}={ctx.resource.name}")["items"]


def load_pending_pods(ctx):
    """Pending pods in every namespace (a cluster-wide list: needs a ClusterRole)."""
    return kubectl_json(ctx, "get", "pods", "-A", "--field-selector", "status.phase=Pending")[
        "items"
    ]


def load_pod_events(ctx):
    """Provisioning events about pods in every namespace, or None when forbidden (reported with ctx.error).

    One cluster-wide list per reason, narrowed on the server: the scheduler's FailedScheduling and
    Karpenter's Nominated and FailedProvisioning events. A list of every pod event would carry a
    Pulled/Created/Started line for each pod of the cluster.
    """
    items = []
    for reason in EVENT_REASONS:
        doc, state = kubectl_json_scoped(
            ctx,
            "events",
            "grant list on events (all namespaces) through a ClusterRole",
            "get",
            "events",
            "-A",
            "--field-selector",
            f"involvedObject.kind=Pod,reason={reason}",
        )
        if state != "ok":
            return None
        items += doc["items"]
    return items
