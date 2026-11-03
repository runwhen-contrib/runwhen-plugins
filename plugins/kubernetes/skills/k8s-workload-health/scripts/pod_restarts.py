from datetime import datetime, timezone

from k8s_workload_health.fetch import load_owned
from k8s_workload_health.workload import cap, pod_restarts


def main(ctx, since="1h"):
    _kind, _workload, _revisions, pods = load_owned(ctx)
    now = datetime.now(timezone.utc)
    restarts = pod_restarts(pods, since, now, ctx.settings.restart_threshold)
    return {
        "restarts": cap(restarts, ctx.settings.max_items),
        "inspected": {"pods": len(pods), "containersWithRestarts": len(restarts)},
    }
