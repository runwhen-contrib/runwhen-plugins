from k8s_artifactory_health import health
from k8s_artifactory_health.fetch import (
    Budget,
    check_inputs,
    load_workload,
    workload_pods,
    workload_services,
)


def main(ctx, port=None, router_port=None, scheme="http"):
    budget = Budget()
    check_inputs(scheme, port, router_port)
    workload = load_workload(ctx)
    pods = workload_pods(ctx, workload)
    services, readable = workload_services(ctx, workload)
    return {
        "health": health.view(
            ctx,
            workload,
            pods,
            services,
            readable,
            scheme,
            port,
            router_port,
            ctx.settings.max_items,
            budget,
        )  # fmt: skip
    }
