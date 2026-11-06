from k8s_jaeger_health import http_errors
from k8s_jaeger_health.common import parse_duration
from k8s_jaeger_health.fetch import Budget, check_inputs, load_workload, workload_pods


def main(ctx, since="1h", limit=100, port=None, scheme="http", services=(), exclude_services=()):
    budget = Budget()
    delta = parse_duration(since)  # bad input fails before any kubectl call
    check_inputs(scheme, port)
    params = {
        "statusCodes": http_errors.check_codes(list(ctx.settings.status_codes)),
        "limit": http_errors.check_limit(limit),
        "services": http_errors.check_names("services", services),
        "excludeServices": http_errors.check_names("excludeServices", exclude_services),
    }
    workload = load_workload(ctx)
    pods = workload_pods(ctx, workload)
    return {
        "httpErrors": http_errors.view(
            ctx,
            workload,
            pods,
            scheme,
            port,
            delta,
            ctx.now(),
            params,
            ctx.settings.max_items,
            budget,
        )
    }
