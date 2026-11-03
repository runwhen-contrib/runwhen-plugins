from k8s_workload_health.fetch import load_owned
from k8s_workload_health.workload import cap, pod_counts, pod_status


def main(ctx):
    _kind, _workload, _revisions, pods = load_owned(ctx)
    return {
        "pods": cap(pod_status(pods), ctx.settings.max_items),
        "counts": pod_counts(pods),
    }
