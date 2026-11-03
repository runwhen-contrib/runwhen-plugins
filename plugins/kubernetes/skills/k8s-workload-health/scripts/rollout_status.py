from k8s_workload_health.fetch import load_revisions, load_workload
from k8s_workload_health.k8s import RwError
from k8s_workload_health.workload import cap, revision_history, rollout_status


def main(ctx):
    kind, workload = load_workload(ctx)
    try:
        revisions = revision_history(kind, load_revisions(ctx, kind, workload))
    except RwError as e:
        # the rollout facts are still good; say which part could not be read
        ctx.error(e.code, e.message, scope="revisions")
        revisions = []
    return {
        "rollout": rollout_status(kind, workload),
        "revisions": cap(revisions, ctx.settings.max_items),
    }
