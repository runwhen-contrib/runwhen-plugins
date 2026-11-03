from k8s_workload_ops.fetch import load_revision_views, load_workload, target_kind
from k8s_workload_ops.k8s import kubectl_text
from k8s_workload_ops.workload import SHORT, rollback_plan, target_view, undo_was_skipped


def main(ctx, to_revision=None):
    kind = target_kind(ctx)
    ns = ctx.resource.namespace
    workload = load_workload(ctx, kind)
    views = load_revision_views(ctx, kind, workload)
    plan = rollback_plan(
        kind, workload, views, to_revision, workload["spec"].get("revisionHistoryLimit")
    )
    dry = bool(ctx.dry_run)
    skipped = False
    if plan["changed"]:
        # always name the revision, so what the approver was shown is what is applied
        undo = [
            "-n",
            ns,
            "rollout",
            "undo",
            f"{SHORT[kind]}/{ctx.resource.name}",
            f"--to-revision={plan['to']['revision']}",
        ]
        output = kubectl_text(ctx, *undo, *(["--dry-run=server"] if dry else []))
        skipped = undo_was_skipped(output)
    return {
        "target": target_view(kind, workload),
        "rollback": {
            "applied": plan["changed"] and not skipped and not dry,
            "changed": plan["changed"] and not skipped,
            "skipped": skipped,
            "from": plan["from"],
            "to": plan["to"],
            "revisionHistoryLimit": workload["spec"].get("revisionHistoryLimit"),
            "available": plan["available"],
        },
    }
