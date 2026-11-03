from k8s_jenkins_health import build_queue
from k8s_jenkins_health.fetch import (
    Budget,
    check_inputs,
    load_workload,
    parse_duration,
    workload_pods,
)


def main(ctx, stuck_after="30m", port=None, scheme="http", url_prefix=""):
    budget = Budget()
    seconds = parse_duration(stuck_after).total_seconds()
    check_inputs(scheme, port, url_prefix)
    workload = load_workload(ctx)
    pods = workload_pods(ctx, workload)
    return {
        "queue": build_queue.view(
            ctx,
            workload,
            pods,
            scheme,
            port,
            seconds,
            ctx.now(),
            ctx.settings.max_items,
            budget,
            url_prefix,
        )
    }
