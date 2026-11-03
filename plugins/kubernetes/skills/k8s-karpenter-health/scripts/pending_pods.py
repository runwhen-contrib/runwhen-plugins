from k8s_karpenter_health.common import parse_duration
from k8s_karpenter_health.fetch import load_pending_pods, load_pod_events, load_pool
from k8s_karpenter_health.pending import pending_pods_for, pod_events


def main(ctx, since="1h"):
    parse_duration(since)  # bad input fails before any kubectl call
    load_pool(ctx)  # the target must exist
    now = ctx.now()
    pods, counts = pending_pods_for(
        load_pending_pods(ctx), ctx.resource.name, now, ctx.settings.max_items
    )

    # Events explain the listed pods. They are a cluster-wide read that can be forbidden on its own,
    # and with no pod listed they are not read: both leave events null ("not read"), never []
    # ("read, none found"). Pods past maxItems are not listed, so they get no events either.
    events = None
    if pods:
        raw = load_pod_events(ctx)
        if raw is not None:
            events = pod_events(raw, pods, since, now, ctx.settings.max_items)
    return {"pods": pods, "counts": counts, "events": events}
