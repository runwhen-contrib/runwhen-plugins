from fetch import load_revisions, load_workload
from workload import cap, revision_history, rollout_status


def main(ctx):
    kind, workload = load_workload(ctx)
    revisions = revision_history(kind, load_revisions(ctx, kind, workload))
    return {
        "rollout": rollout_status(kind, workload),
        "revisions": cap(revisions, ctx.settings.max_items),
    }
