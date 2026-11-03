from k8s_prometheus_health.fetch import load_prometheus, prometheus_pods
from k8s_prometheus_health.status import collect


def main(ctx):
    prom = load_prometheus(ctx)
    pods = prometheus_pods(ctx, prom)
    return {"server": collect(ctx, prom, pods, ctx.now(), ctx.settings.max_items)}
