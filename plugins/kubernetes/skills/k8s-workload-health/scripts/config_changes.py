from datetime import datetime, timezone

from fetch import load_revisions, load_workload
from workload import cap, config_changes


def main(ctx, since="1h"):
    kind, workload = load_workload(ctx)
    revisions = load_revisions(ctx, kind, workload)
    now = datetime.now(timezone.utc)
    return {"changes": cap(config_changes(kind, workload, revisions, since, now), ctx.settings.max_items)}
