from k8s_prometheus_health import rules
from k8s_prometheus_health.fetch import load_prometheus, prometheus_pods, query_pods


def main(ctx):
    prom = load_prometheus(ctx)
    pods = prometheus_pods(ctx, prom)
    answers = query_pods(ctx, prom, pods, rules.PATH, "rules", rules.parse_reply)
    return {
        "rules": rules.view(
            answers, ctx.now(), ctx.settings.max_items, ctx.settings.overdue_intervals
        )
    }
