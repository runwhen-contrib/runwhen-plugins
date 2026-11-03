from k8s_litellm_health import health
from k8s_litellm_health.fetch import Budget, check_inputs, load_workload, workload_pods


def main(ctx, port=None, scheme="http"):
    budget = Budget()
    check_inputs(scheme, port)
    workload = load_workload(ctx)
    pods = workload_pods(ctx, workload)
    return {
        "liveness": health.view(
            ctx, "liveness", workload, pods, scheme, port, ctx.settings.max_items, budget
        )
    }
