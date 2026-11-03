from k8s_prometheus_health import targets
from k8s_prometheus_health.fetch import (
    Budget,
    load_prometheus,
    pod_stats,
    prometheus_pods,
    query_pods,
)


def main(ctx):
    budget = Budget()
    prom = load_prometheus(ctx)
    pods = prometheus_pods(ctx, prom)
    answers = query_pods(ctx, prom, pods, targets.PATH, "targets", targets.parse_reply, budget)
    view = targets.view(answers, ctx.now(), ctx.settings.max_items, ctx.settings.overdue_intervals)
    return {"targets": {**view, **pod_stats(pods)}}
