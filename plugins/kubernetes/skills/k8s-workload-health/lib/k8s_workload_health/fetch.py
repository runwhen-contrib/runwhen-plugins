"""Fetching the target workload and the objects it owns (the only I/O besides k8s.py)."""

from k8s_workload_health.k8s import RwError, kubectl_json
from k8s_workload_health.workload import (
    DAEMONSET,
    DEPLOYMENT,
    STATEFULSET,
    owned_pods,
    owned_revisions,
    selector_string,
)

# ctx.resource.type -> (Kind, kubectl resource)
TARGETS = {
    "kubernetes/deployment": (DEPLOYMENT, "deployments.apps"),
    "kubernetes/statefulset": (STATEFULSET, "statefulsets.apps"),
    "kubernetes/daemonset": (DAEMONSET, "daemonsets.apps"),
}
# the plural segment of ctx.resource.path -> same
TARGETS_BY_PLURAL = {
    resource.split(".")[0]: (kind, resource) for kind, resource in TARGETS.values()
}


def _target(ctx):
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        if rtype not in TARGETS:
            raise RwError("invalid_input", f"k8s-workload-health does not apply to {rtype!r}")
        return TARGETS[rtype]
    # no type: the path ends .../<plural>/<name>
    parts = (getattr(ctx.resource, "path", None) or "").strip("/").split("/")
    plural = parts[-2] if len(parts) >= 2 else None
    if plural not in TARGETS_BY_PLURAL:
        raise RwError(
            "invalid_input",
            "cannot tell the target kind: no resource type, and the path does not end in <plural>/<name> of a Deployment, StatefulSet or DaemonSet",
        )
    return TARGETS_BY_PLURAL[plural]


def target_kind(ctx):
    return _target(ctx)[0]


def load_workload(ctx):
    """(kind, workload). A missing target raises RwError(not_found)."""
    kind, resource = _target(ctx)
    return kind, kubectl_json(ctx, "-n", ctx.resource.namespace, "get", resource, ctx.resource.name)


def load_revisions(ctx, kind, workload):
    """The workload's ReplicaSets (Deployment) or ControllerRevisions."""
    resource = "replicasets.apps" if kind == DEPLOYMENT else "controllerrevisions.apps"
    selector = selector_string(workload["spec"]["selector"])
    items = kubectl_json(ctx, "-n", ctx.resource.namespace, "get", resource, "-l", selector)[
        "items"
    ]
    return owned_revisions(kind, workload, items)


def load_pods(ctx, kind, workload, revisions):
    selector = selector_string(workload["spec"]["selector"])
    items = kubectl_json(ctx, "-n", ctx.resource.namespace, "get", "pods", "-l", selector)["items"]
    return owned_pods(kind, workload, revisions, items)


def load_owned(ctx, want_revisions=False):
    """(kind, workload, revisions, pods) for the target.

    Revisions are fetched only when a Deployment's pods need them for
    ownership (Deployment -> ReplicaSet -> Pod) or the caller asks for them.
    """
    kind, workload = load_workload(ctx)
    revisions = []
    if kind == DEPLOYMENT or want_revisions:
        revisions = load_revisions(ctx, kind, workload)
    return kind, workload, revisions, load_pods(ctx, kind, workload, revisions)
