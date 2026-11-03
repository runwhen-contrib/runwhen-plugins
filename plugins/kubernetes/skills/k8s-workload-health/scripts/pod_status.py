from fetch import load_owned
from workload import cap, pod_counts, pod_status


def main(ctx):
    _kind, _workload, _revisions, pods = load_owned(ctx)
    return {
        "pods": cap(pod_status(pods), ctx.settings.max_items),
        "counts": pod_counts(pods),
    }
