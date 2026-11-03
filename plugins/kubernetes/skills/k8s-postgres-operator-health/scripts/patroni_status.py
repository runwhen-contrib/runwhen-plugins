from k8s_postgres_operator_health.fetch import (
    load_cluster,
    no_member_pod,
    patroni_members_text,
    queried,
    require_pods,
    target_kind,
)
from k8s_postgres_operator_health.operator import (
    exec_candidates,
    parse_patroni,
    patroni_unavailable,
    patroni_view,
)


def main(ctx):
    kind = target_kind(ctx)
    load_cluster(ctx, kind)  # the target must exist
    threshold = ctx.settings.lag_threshold_mb
    candidates = exec_candidates(kind, require_pods(ctx, kind))
    if not candidates:
        no_member_pod(ctx, kind, "patronictl list")
        return {"patroni": patroni_unavailable(threshold)}
    pod = candidates[0]
    members = parse_patroni(patroni_members_text(ctx, kind, pod))
    return {"patroni": patroni_view(members, queried(kind, pod), threshold, ctx.settings.max_items)}
