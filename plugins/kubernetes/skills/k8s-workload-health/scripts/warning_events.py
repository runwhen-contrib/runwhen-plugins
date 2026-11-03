from datetime import datetime, timezone

from k8s_workload_health.fetch import load_owned
from k8s_workload_health.k8s import kubectl_json
from k8s_workload_health.workload import related_names, warning_events


def main(ctx, since="1h"):
    kind, workload, revisions, pods = load_owned(ctx)
    events = kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "events", "--field-selector", "type=Warning"
    )["items"]
    now = datetime.now(timezone.utc)
    names = related_names(kind, workload, revisions, pods)
    return {"events": warning_events(events, names, since, now, ctx.settings.max_items)}
