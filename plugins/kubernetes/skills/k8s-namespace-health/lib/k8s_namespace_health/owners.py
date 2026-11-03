"""Resolving which workload owns a pod, ReplicaSet or Job.

Pods hang off their workload through ownerReferences: Pod -> ReplicaSet ->
Deployment, Pod -> StatefulSet / DaemonSet, Pod -> Job -> CronJob. The chain is
walked through the objects listed from the namespace; an owner that is no longer
there is reported as such instead of being dropped.
"""

# Kinds whose own controller has to be looked up to find the top-level workload.
INTERMEDIATE = ("ReplicaSet", "Job")


def controller_ref(obj):
    for ref in obj.get("metadata", {}).get("ownerReferences") or []:
        if ref.get("controller"):
            return ref
    return None


def index_objects(**by_kind):
    """{(kind, name): object} for lists keyed by kind, e.g. Pod=[...], Job=[...]."""
    return {(kind, o["metadata"]["name"]): o for kind, items in by_kind.items() for o in items}


def resolve_owner(obj, index):
    """The top-level controller of `obj` as {kind, name, resolved}, or None when
    it has no controller. `resolved` is false when the chain stops at a
    ReplicaSet or Job that no longer exists (the immediate owner is returned)."""
    ref = controller_ref(obj)
    for _ in range(8):
        if ref is None:
            return None
        kind, name = ref["kind"], ref["name"]
        if kind not in INTERMEDIATE:
            return {"kind": kind, "name": name, "resolved": True}
        parent = index.get((kind, name))
        if parent is None:
            return {"kind": kind, "name": name, "resolved": False}
        up = controller_ref(parent)
        if up is None:
            return {"kind": kind, "name": name, "resolved": True}
        ref = up
    raise ValueError(f"ownerReferences chain of {obj['metadata']['name']!r} is too deep")
