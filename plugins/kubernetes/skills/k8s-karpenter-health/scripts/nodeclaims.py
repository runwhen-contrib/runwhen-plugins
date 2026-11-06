from k8s_karpenter_health.fetch import load_nodeclaims, load_pool
from k8s_karpenter_health.nodeclaims import nodeclaims_for


def main(ctx):
    pool = load_pool(ctx)
    items = load_nodeclaims(ctx)
    views, counts = nodeclaims_for(
        items, pool, ctx.now(), ctx.settings.stuck_minutes, ctx.settings.max_items
    )
    return {"nodeClaims": views, "counts": counts}
