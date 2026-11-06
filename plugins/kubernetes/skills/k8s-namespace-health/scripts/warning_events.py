from k8s_namespace_health.clock import reference_time
from k8s_namespace_health.events import warning_events
from k8s_namespace_health.fetch import (
    check_duration,
    check_settings,
    list_items,
    list_secondary,
    load_namespace,
)


def main(ctx, since="1h"):
    check_duration(since)
    check_settings(ctx, "max_items")
    namespace = load_namespace(ctx)
    events = list_items(ctx, namespace, "events", "--field-selector", "type=Warning")
    pods = list_secondary(ctx, namespace, "pods")
    replicasets = list_secondary(ctx, namespace, "replicasets.apps")
    jobs = list_secondary(ctx, namespace, "jobs.batch")
    now = reference_time(ctx)
    return {
        "events": warning_events(
            events, pods, replicasets, jobs, since, now, ctx.settings.max_items
        )
    }
