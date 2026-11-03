from datetime import datetime, timezone

from k8s_karpenter_health.common import parse_duration
from k8s_karpenter_health.fetch import load_pending_pods, load_pod_events, load_pool
from k8s_karpenter_health.pending import pending_pods_for, pod_events


def main(ctx, since="1h"):
    parse_duration(since)  # bad input fails before any kubectl call
    load_pool(ctx)  # the target must exist
    now = datetime.now(timezone.utc)
    pods, counts = pending_pods_for(
        load_pending_pods(ctx), ctx.resource.name, now, ctx.settings.max_items
    )

    # Events explain the pods; they are a cluster-wide read that can be forbidden on its own, in which
    # case the pods stand and events is null. With no pod to explain they are not read at all.
    events = []
    if pods:
        raw = load_pod_events(ctx)
        events = None if raw is None else pod_events(raw, pods, since, now, ctx.settings.max_items)
    return {"pods": pods, "counts": counts, "events": events}
