from k8s_workload_ops.fetch import load_owned_pods, load_workload, target_kind
from k8s_workload_ops.k8s import MESSAGE_LIMIT, failure, run_unchecked
from k8s_workload_ops.workload import cap, delete_plan, parse_deleted, target_view

BATCH = 50  # pod names per kubectl call


def main(ctx, pod_names=None):
    kind = target_kind(ctx)
    ns = ctx.resource.namespace
    workload = load_workload(ctx, kind)
    plan = delete_plan(load_owned_pods(ctx, kind, workload), pod_names)
    names = [p["name"] for p in plan["pods"]]
    dry = bool(ctx.dry_run)
    confirmed, gone, failed = [], [], None
    for start in range(0, len(names), BATCH):
        batch = names[start : start + BATCH]
        argv = ["kubectl", "-n", ns, "delete", "pod", *batch]
        argv += ["--wait=false", "--ignore-not-found", "-o", "name"]
        if dry:
            argv.append("--dry-run=server")
        # check=False: a failing batch may still have deleted some of its pods, and their names are
        # on stdout; they are reported, not lost behind the exception
        result = run_unchecked(ctx, argv)
        done = parse_deleted(result.stdout)
        confirmed += done
        if result.returncode:
            failed = failure(argv, result)
            break
        # --ignore-not-found: a requested pod kubectl did not print was already gone
        gone += [n for n in batch if n not in done]
    remaining = len(names) - len(confirmed) - len(gone)
    if failed is not None:
        if not confirmed:
            raise failed
        ctx.error(
            failed.code,
            (
                f"stopped after {len(confirmed)} of {len(names)} pods; {remaining} requested "
                f"pods remain: {failed.message}"
            )[:MESSAGE_LIMIT],
            scope="delete",
        )
    return {
        "target": target_view(kind, workload),
        "deletion": {
            "applied": bool(confirmed) and not dry,
            "ownedCount": plan["ownedCount"],
            "requestedCount": len(names),
            "deletedCount": 0 if dry else len(confirmed),
            "wouldDeleteCount": len(confirmed) if dry else 0,
            "alreadyGoneCount": len(gone),
            "remainingCount": remaining,
            "deletesAll": plan["deletesAll"],
            "deleted": [] if dry else cap(confirmed, ctx.settings.max_items),
            "wouldDelete": cap(confirmed, ctx.settings.max_items) if dry else [],
            "alreadyGone": cap(gone, ctx.settings.max_items),
            "pods": cap(plan["pods"], ctx.settings.max_items),
        },
    }
