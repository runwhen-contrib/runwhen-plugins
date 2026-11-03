from k8s_postgres_operator_ops.fetch import (
    Budget,
    load_cluster,
    load_pods,
    pick_exec_pod,
    read_members,
    run_patronictl,
    target_kind,
    wait_until,
)
from k8s_postgres_operator_ops.operator import (
    KINDS,
    ReinitWatch,
    check_reinit,
    check_timeout,
    clean_member,
    patroni_cluster_name,
    target_view,
)


def main(ctx, member, timeout="120s"):
    budget = Budget(ctx)
    kind = target_kind(ctx)
    member = clean_member(member)
    wait = check_timeout(timeout)
    cluster = load_cluster(ctx, kind)
    pod = pick_exec_pod(ctx, kind, load_pods(ctx, kind), "patronictl", avoid=(member,))
    raw, views = read_members(ctx, kind, pod)
    scope = patroni_cluster_name(raw)
    before = check_reinit(views, member)  # refuses the leader, an unknown member, no healthy leader
    command = {"tool": "patronictl", "subcommand": "reinit", "cluster": scope, "member": member}
    dry = bool(ctx.dry_run)

    out = {
        "applied": False,
        "wouldRun": dry,
        "member": member,
        "role": before["role"],
        "stateBefore": before["state"],
        "lagBeforeMb": before["lagMb"],
        "leader": next((m["name"] for m in views if m["leader"]), None),
        "command": command,
        "ranIn": {"pod": pod["metadata"]["name"], "container": KINDS[kind]["container"]},
        "output": None,
        "outputTruncated": False,
        "stateAfter": None,
        "roleAfter": None,
        "completed": None,
        "completionReason": None,
        "waitedSeconds": None,
    }
    if not dry:
        sent = run_patronictl(
            ctx, kind, pod, budget, "reinit", member, "reinit", scope, member, "--force",
            scope="reinit",
        )  # fmt: skip
        out["output"], out["outputTruncated"] = sent["output"], sent["truncated"]
        if not sent["ok"]:
            # no Success line, a Failed line, or patronictl still running at the time limit: the
            # re-clone is not confirmed (ctx.error has said why), so it is not reported as applied
            out["completed"] = False
            out["completionReason"] = "unconfirmed" if sent["code"] == "timeout" else "failed"
            return {"target": target_view(kind, cluster, scope), "reinit": out}
        out["applied"] = True
        if wait <= 0:
            out["completionReason"] = "not_waited"
        else:
            # Reinit only starts the re-clone; poll the member until it runs or streams again.
            seconds, capped = budget.wait_for(wait)
            watch = ReinitWatch(member)
            last, reason, waited = wait_until(
                ctx, seconds, lambda: read_members(ctx, kind, pod)[1], watch
            )
            out["waitedSeconds"] = waited
            if last is not None:
                after = next((m for m in last if m["name"] == member), None)
                out["stateAfter"] = after["state"] if after else None
                out["roleAfter"] = after["role"] if after else None
            out["completed"] = None if reason == "not_observed" else reason == "done"
            out["completionReason"] = (
                "running"
                if reason == "done"
                else ("budget" if reason == "timeout" and capped else reason)
            )
    return {"target": target_view(kind, cluster, scope), "reinit": out}
