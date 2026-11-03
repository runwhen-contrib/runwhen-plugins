from k8s_postgres_operator_ops.fetch import (
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
    check_timeout,
    clean_member,
    leader_change_done,
    leader_of,
    patroni_cluster_name,
    plan_switchover,
    target_view,
)


def main(ctx, candidate=None, timeout="60s"):
    kind = target_kind(ctx)
    wait = check_timeout(timeout)
    candidate = clean_member(candidate, "candidate") if candidate else None
    cluster = load_cluster(ctx, kind)
    pods = load_pods(ctx, kind)
    # Run patronictl in a pod other than the leader's when there is one: the leader is demoted.
    first = pick_exec_pod(ctx, kind, pods, "patronictl")
    raw, views = read_members(ctx, kind, first)
    old = leader_of(views)
    pod = pick_exec_pod(ctx, kind, pods, "patronictl", avoid=(old["name"],) if old else ())
    if pod is not first:
        raw, views = read_members(ctx, kind, pod)
        old = leader_of(views)
    scope = patroni_cluster_name(raw)
    mode, leader, chosen, chosen_by = plan_switchover(views, candidate)
    args = [mode, scope]
    if chosen is not None:
        args += ["--candidate", chosen["name"]]
    args.append("--force")
    command = {
        "tool": "patronictl",
        "subcommand": mode,
        "cluster": scope,
        "candidate": chosen["name"] if chosen else None,
    }
    dry = bool(ctx.dry_run)

    out = {
        "applied": not dry,
        "wouldRun": dry,
        "mode": mode,
        "leaderBefore": leader["name"] if leader else None,
        "leaderBeforeState": leader["state"] if leader else None,
        "timelineBefore": leader["timeline"] if leader else None,
        "candidate": chosen["name"] if chosen else None,
        "candidateChosenBy": chosen_by,
        "command": command,
        "ranIn": {"pod": pod["metadata"]["name"], "container": KINDS[kind]["container"]},
        "output": None,
        "outputTruncated": False,
        "leaderAfter": None,
        "leaderAfterState": None,
        "timelineAfter": None,
        "leaderChanged": None,
        "completed": None,
        "completionReason": None,
        "waitedSeconds": None,
        "membersAfter": None,
    }
    if not dry:
        out["output"], out["outputTruncated"] = run_patronictl(ctx, kind, pod, *args)
        last, reason, waited = None, "not_waited", None
        if wait > 0:
            last, reason, waited = wait_until(
                ctx,
                wait,
                lambda: read_members(ctx, kind, pod)[1],
                lambda v: leader_change_done(v, mode, leader, chosen),
            )
        out["waitedSeconds"] = waited
        if last is None:
            out["completionReason"] = "not_waited" if wait <= 0 else "not_observed"
        else:
            now = leader_of(last)
            changed = bool(now and (leader is None or now["name"] != leader["name"]))
            out.update(
                leaderAfter=now["name"] if now else None,
                leaderAfterState=now["state"] if now else None,
                timelineAfter=now["timeline"] if now else None,
                leaderChanged=changed,
                completed=reason == "done",
                completionReason={"done": "leader_changed"}.get(reason, reason),
                membersAfter=[
                    {k: m[k] for k in ("name", "role", "state", "timeline", "lagMb")}
                    for m in last[:50]
                ],
            )
            if reason == "not_observed":
                out["completed"] = None
    return {"target": target_view(kind, cluster, scope), "switchover": out}
