from k8s_workload_health.clock import reference_time
from k8s_workload_health.fetch import load_owned
from k8s_workload_health.k8s import kubectl_json
from k8s_workload_health.workload import parse_duration, pod_uids, related_names, warning_events


def main(ctx, since="1h"):
    window = parse_duration(since)
    kind, workload, revisions, pods = load_owned(ctx, want_revisions=True)
    events = kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "events", "--field-selector", "type=Warning"
    )["items"]
    now = reference_time(ctx)
    names = related_names(kind, workload, revisions, pods)
    return {
        "events": warning_events(
            events, names, window, now, ctx.settings.max_items, pod_uids=pod_uids(pods)
        )
    }
