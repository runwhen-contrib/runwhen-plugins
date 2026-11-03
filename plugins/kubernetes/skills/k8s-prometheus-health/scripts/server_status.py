from k8s_prometheus_health.fetch import Budget, load_prometheus, prometheus_pods
from k8s_prometheus_health.status import collect


def main(ctx):
    budget = Budget()
    prom = load_prometheus(ctx)
    pods = prometheus_pods(ctx, prom)
    return {"server": collect(ctx, prom, pods, ctx.now(), ctx.settings.max_items, budget)}
