from k8s_postgres_operator_health.fetch import load_cluster, load_pods, selector, target_kind
from k8s_postgres_operator_health.operator import backups_view, cluster_view, members_view


def main(ctx):
    kind = target_kind(ctx)
    obj = load_cluster(ctx, kind)
    pods = load_pods(ctx, kind)
    return {
        "cluster": cluster_view(kind, obj, pods),
        "backups": backups_view(kind, obj, ctx.now()),
        # null (with a permission_denied error) when the pods could not be listed: not "no members"
        "members": None
        if pods is None
        else members_view(kind, pods, selector(ctx, kind), ctx.settings.max_items),
    }
