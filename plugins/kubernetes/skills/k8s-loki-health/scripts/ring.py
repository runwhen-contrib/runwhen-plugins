from k8s_loki_health import ring
from k8s_loki_health.fetch import check_inputs, load_workload, workload_pods


def main(ctx, port=None, scheme="http"):
    paths = list(ctx.settings.ring_paths)
    check_inputs(scheme, port, paths)
    workload = load_workload(ctx)
    pods = workload_pods(ctx, workload)
    return {
        "rings": ring.collect(
            ctx, workload, pods, scheme, port, paths, ctx.now(), ctx.settings.max_items
        )
    }
