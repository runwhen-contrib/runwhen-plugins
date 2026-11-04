from k8s_jenkins_health import http_status
from k8s_jenkins_health.fetch import Budget, check_inputs, load_workload, workload_pods


def main(ctx, port=None, scheme=None, url_prefix=None):
    budget = Budget()
    check_inputs(scheme, port, url_prefix)
    workload = load_workload(ctx)
    pods = workload_pods(ctx, workload)
    return {
        "login": http_status.view(
            ctx, workload, pods, scheme, port, ctx.settings.max_items, budget, url_prefix
        )
    }
