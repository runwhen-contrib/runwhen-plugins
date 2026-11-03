from k8s_karpenter_health.fetch import load_nodeclass, load_pool
from k8s_karpenter_health.nodepool import limits_view, nodeclass_lookup, nodeclass_view, pool_view


def main(ctx):
    pool = load_pool(ctx)

    # The NodeClass is read apart from the pool: a NodeClass is cluster-scoped and its type depends
    # on the cloud provider, so a forbidden or unknown one must not cost the pool's own facts.
    plural_group, name = nodeclass_lookup(pool)
    obj, state = None, "external"
    if plural_group:
        obj, state = load_nodeclass(ctx, plural_group, name)
    return {
        "pool": pool_view(pool),
        "limits": limits_view(pool, ctx.settings.limit_warning_percent),
        "nodeClass": nodeclass_view(pool, obj, state),
    }
