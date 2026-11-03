"""Fetching the target workload and the objects it owns (the only I/O besides k8s.py)."""

from k8s_workload_health.k8s import kubectl_json
from k8s_workload_health.workload import DAEMONSET, DEPLOYMENT, STATEFULSET, owned_pods, owned_revisions, selector_string

# ctx.resource.type -> (Kind, kubectl resource)
TARGETS = {
    "kubernetes/deployment": (DEPLOYMENT, "deployments.apps"),
    "kubernetes/statefulset": (STATEFULSET, "statefulsets.apps"),
    "kubernetes/daemonset": (DAEMONSET, "daemonsets.apps"),
}


def target_kind(ctx):
    try:
        return TARGETS[ctx.resource.type][0]
    except KeyError:
        raise ValueError(f"k8s-workload-health does not apply to {ctx.resource.type!r}") from None


def load_workload(ctx):
    """(kind, workload). A missing target raises (NotFound from kubectl)."""
    kind = target_kind(ctx)
    resource = TARGETS[ctx.resource.type][1]
    return kind, kubectl_json(ctx, "-n", ctx.resource.namespace, "get", resource, ctx.resource.name)


def load_revisions(ctx, kind, workload):
    """The workload's ReplicaSets (Deployment) or ControllerRevisions."""
    resource = "replicasets.apps" if kind == DEPLOYMENT else "controllerrevisions.apps"
    selector = selector_string(workload["spec"]["selector"])
    items = kubectl_json(ctx, "-n", ctx.resource.namespace, "get", resource, "-l", selector)["items"]
    return owned_revisions(kind, workload, items)


def load_pods(ctx, kind, workload, revisions):
    selector = selector_string(workload["spec"]["selector"])
    items = kubectl_json(ctx, "-n", ctx.resource.namespace, "get", "pods", "-l", selector)["items"]
    return owned_pods(kind, workload, revisions, items)


def load_owned(ctx):
    """(kind, workload, revisions, pods) for the target."""
    kind, workload = load_workload(ctx)
    revisions = load_revisions(ctx, kind, workload)
    return kind, workload, revisions, load_pods(ctx, kind, workload, revisions)
