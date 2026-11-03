from k8s_loki_health import ready
from k8s_loki_health.fetch import check_inputs, load_workload, workload_pods


def main(ctx, port=None, scheme="http"):
    check_inputs(scheme, port)
    workload = load_workload(ctx)
    pods = workload_pods(ctx, workload)
    return {"ready": ready.view(ctx, workload, pods, scheme, port, ctx.settings.max_items)}
