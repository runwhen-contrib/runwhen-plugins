from datetime import UTC, datetime

from k8s_workload_health.fetch import load_revisions, load_workload
from k8s_workload_health.workload import cap, config_changes, parse_duration


def main(ctx, since="1h"):
    window = parse_duration(since)
    kind, workload = load_workload(ctx)
    revisions = load_revisions(ctx, kind, workload)
    now = datetime.now(UTC)
    changes = config_changes(kind, workload, revisions, window, now)
    return {"changes": cap(changes, ctx.settings.max_items)}
