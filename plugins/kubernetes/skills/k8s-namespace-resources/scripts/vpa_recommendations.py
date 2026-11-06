from k8s_namespace_resources.fetch import (
    check_settings,
    list_items,
    list_optional,
    load_namespace,
)
from k8s_namespace_resources.vpa import intermediate_resources, target_resources, vpa_entries


def main(ctx):
    check_settings(ctx, "max_items", "vpa_difference_percent")
    ns = load_namespace(ctx, "vpa-recommendations")
    # A missing VerticalPodAutoscaler CRD raises `dependency`; it never reads as "no recommendations".
    vpas = list_items(ctx, ns, "verticalpodautoscalers.autoscaling.k8s.io")
    # Current requests: one list per workload kind the VPAs target. A kind that cannot be read leaves
    # `targetFound` and `current` unknown for its VPAs.
    workloads = {}
    for resource in target_resources(vpas):
        items = list_optional(ctx, ns, resource.split(".")[0], resource)
        if items is not None:
            workloads[resource] = items
    # The live pods' requests (`currentLive`): the pods, and the ReplicaSets / Jobs that say which are
    # the target's. If any of them cannot be read, currentLive is null rather than a wrong zero.
    pods = list_optional(ctx, ns, "pods", "pods") if vpas else None
    owners = {}
    for resource in intermediate_resources(vpas) if pods is not None else []:
        items = workloads.get(resource)
        if items is None:
            items = list_optional(ctx, ns, resource.split(".")[0], resource)
        if items is None:
            pods = None
            break
        owners[resource] = items
    entries = vpa_entries(
        vpas,
        workloads,
        ctx.settings.vpa_difference_percent,
        ctx.settings.max_items,
        pods=pods,
        replicasets=owners.get("replicasets.apps"),
        jobs=owners.get("jobs.batch"),
    )
    return {"vpas": entries, "inspected": {"vpas": len(vpas)}}
