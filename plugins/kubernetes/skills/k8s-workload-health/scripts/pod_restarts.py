from k8s_workload_health.clock import reference_time
from k8s_workload_health.fetch import load_owned
from k8s_workload_health.workload import cap, parse_duration, pod_restarts


def main(ctx, since="1h"):
    window = parse_duration(since)
    _kind, _workload, _revisions, pods = load_owned(ctx)
    now = reference_time(ctx)
    restarts = pod_restarts(pods, window, now, ctx.settings.restart_threshold)
    return {
        "restarts": cap(restarts, ctx.settings.max_items),
        "inspected": {"pods": len(pods), "containersWithRestarts": len(restarts)},
    }
