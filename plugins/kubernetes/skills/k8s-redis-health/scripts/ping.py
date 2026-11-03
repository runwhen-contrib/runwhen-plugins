from k8s_redis_health.check import ping_instances
from k8s_redis_health.fetch import load_pods, load_workload, target
from k8s_redis_health.workload import cap


def main(ctx, container=None):
    kind, resource = target(ctx)
    workload = load_workload(ctx, resource)
    pods = load_pods(ctx, kind, workload)
    instances, counts = ping_instances(ctx, pods, container, ctx.settings.memory_percent_threshold)
    return {"instances": cap(instances, ctx.settings.max_items), "counts": counts}
