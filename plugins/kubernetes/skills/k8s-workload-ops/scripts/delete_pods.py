from k8s_workload_ops.errors import RwError
from k8s_workload_ops.fetch import load_owned_pods, load_workload, target_kind
from k8s_workload_ops.k8s import MESSAGE_LIMIT, kubectl_text
from k8s_workload_ops.workload import cap, delete_plan, parse_deleted, target_view

BATCH = 50  # pod names per kubectl call


def main(ctx, pod_names=None):
    kind = target_kind(ctx)
    ns = ctx.resource.namespace
    workload = load_workload(ctx, kind)
    plan = delete_plan(load_owned_pods(ctx, kind, workload), pod_names)
    names = [p["name"] for p in plan["pods"]]
    dry = bool(ctx.dry_run)
    deleted = []
    for start in range(0, len(names), BATCH):
        batch = names[start : start + BATCH]
        try:
            out = kubectl_text(
                ctx,
                "-n",
                ns,
                "delete",
                "pod",
                *batch,
                "--wait=false",
                "-o",
                "name",
                *(["--dry-run=server"] if dry else []),
            )
        except RwError as exc:
            if not deleted:
                raise
            # earlier batches are already gone: keep their names and report the failure
            ctx.error(
                exc.code,
                f"stopped after {len(deleted)} pods: {exc.message}"[:MESSAGE_LIMIT],
                scope="delete",
            )
            break
        deleted += parse_deleted(out)
    return {
        "target": target_view(kind, workload),
        "deletion": {
            "applied": bool(deleted) and not dry,
            "ownedCount": plan["ownedCount"],
            "requestedCount": len(names),
            "deletedCount": len(deleted),
            "deletesAll": plan["deletesAll"],
            "deleted": cap(deleted, ctx.settings.max_items),
            "pods": cap(plan["pods"], ctx.settings.max_items),
        },
    }
