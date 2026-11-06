from k8s_redis_health.check import read_write_check
from k8s_redis_health.clock import reference_time
from k8s_redis_health.fetch import Conn, load_pods, load_workload, target
from k8s_redis_health.workload import cap


def main(ctx, container=None, port=6379):
    conn = Conn(port)  # validates the port and starts the 45s budget before any kubectl call
    kind, resource = target(ctx)
    workload = load_workload(ctx, resource)
    pods = load_pods(ctx, kind, workload)
    candidates, check = read_write_check(
        ctx, pods, container, reference_time(ctx), bool(ctx.dry_run), conn
    )
    return {"candidates": cap(candidates, ctx.settings.max_items), "check": check}
