from k8s_istio_health.common import bound, check_int_setting
from k8s_istio_health.errors import RwError
from k8s_istio_health.fetch import list_scoped, load_namespace
from k8s_istio_health.injection import (
    KINDS,
    namespace_injection,
    other_pods,
    pods_of,
    sort_workloads,
    workload_view,
)


def main(ctx):
    max_items = check_int_setting("maxItems", ctx.settings.max_items, 1, 10000)
    ns = load_namespace(ctx)  # not found raises; forbidden -> injection.enabled None
    injection = namespace_injection(ns, ctx.resource.name)

    # One list per workload kind. A kind that is forbidden is an error on the result and the
    # others are still reported; when none can be listed there is nothing to report.
    lists = {kind: list_scoped(ctx, plural, f"{plural}.apps") for kind, plural in KINDS}
    if all(items is None for items in lists.values()):
        raise RwError(
            "permission_denied",
            f"not allowed to list deployments, statefulsets or daemonsets in {ctx.resource.name}",
        )
    pods = list_scoped(ctx, "pods", "pods")
    replicasets = None
    if pods is not None and lists["Deployment"]:
        replicasets = list_scoped(ctx, "replicasets", "replicasets.apps")

    views, claimed = [], set()
    for kind, _plural in KINDS:
        for workload in lists[kind] or []:
            owned = None if pods is None else pods_of(kind, workload, replicasets, pods)
            claimed |= {p["metadata"]["name"] for p in owned or []}
            views.append(workload_view(kind, workload, owned, injection))
    workloads, total, omitted = bound(sort_workloads(views), max_items)
    return {
        "injection": injection,
        "workloads": workloads,
        "inspected": {
            "namespace": ctx.resource.name,
            "namespaceRead": ns is not None,
            "deployments": None if lists["Deployment"] is None else len(lists["Deployment"]),
            "statefulsets": None if lists["StatefulSet"] is None else len(lists["StatefulSet"]),
            "daemonsets": None if lists["DaemonSet"] is None else len(lists["DaemonSet"]),
            "pods": None if pods is None else len(pods),
            "podsOutsideWorkloads": None if pods is None else other_pods(pods, claimed),
            "total": {"workloads": total},
            "omitted": {"workloads": omitted},
        },
    }
