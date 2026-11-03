import json

from k8s_postgres_operator_ops.errors import RwError
from k8s_postgres_operator_ops.fetch import (
    Budget,
    load_cluster,
    load_pods,
    pick_exec_pod,
    read_members,
    target_kind,
    wait_until,
)
from k8s_postgres_operator_ops.flow import restart_members
from k8s_postgres_operator_ops.k8s import kubectl_text
from k8s_postgres_operator_ops.operator import (
    CRUNCHY,
    KINDS,
    RESTART_ANNOTATION,
    check_timeout,
    crunchy_restart_done,
    desired_instances,
    limit_text,
    patroni_cluster_name,
    pod_summary,
    restart_patch,
    target_view,
)


def main(ctx, timeout="120s"):
    budget = Budget(ctx)
    kind = target_kind(ctx)
    wait = check_timeout(timeout)
    if kind != CRUNCHY and wait <= 0:
        raise RwError(
            "invalid_input",
            "timeout must be at least 1s for a Zalando restart: each member has to be back before the next is restarted",
        )
    cluster = load_cluster(ctx, kind)
    dry = bool(ctx.dry_run)
    if kind == CRUNCHY:
        return crunchy(ctx, kind, cluster, wait, dry, budget)
    return zalando(ctx, kind, cluster, wait, dry, budget)


def crunchy(ctx, kind, cluster, wait, dry, budget):
    """Set the restart annotation on spec.metadata.annotations; the operator rolls the pods."""
    ns = ctx.resource.namespace
    requested_at = ctx.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    desired = desired_instances(cluster)
    previous = (((cluster.get("spec") or {}).get("metadata") or {}).get("annotations") or {}).get(
        RESTART_ANNOTATION
    )
    kubectl_text(
        ctx,
        "-n",
        ns,
        "patch",
        KINDS[kind]["resource"],
        ctx.resource.name,
        "--type=merge",
        "-p",
        json.dumps(restart_patch(requested_at)),
        *(["--dry-run=server"] if dry else []),
    )
    out = base(dry, "annotation", requested_at, desired)
    out.update(annotation=RESTART_ANNOTATION, previousAnnotation=limit_text(previous)[0])
    if not dry and wait <= 0:
        out["completionReason"] = "not_waited"
    elif not dry:
        # The restart is the operator's: completed only when every instance pod it asks for is
        # Ready and was restarted at or after this request, not merely Running (it may not have
        # begun yet).
        def observe():
            return [pod_summary(kind, p) for p in load_pods(ctx, kind)]

        seconds, capped = budget.wait_for(wait)
        last, reason, waited = wait_until(
            ctx,
            seconds,
            observe,
            lambda s, _elapsed: crunchy_restart_done(s, desired, requested_at)[0],
        )
        out["waitedSeconds"] = waited
        if last is None:
            out["completionReason"] = "not_observed"
        else:
            _, restarted, ready = crunchy_restart_done(last, desired, requested_at)
            out.update(
                completed=None if reason == "not_observed" else reason == "done",
                completionReason="restarted"
                if reason == "done"
                else ("budget" if reason == "timeout" and capped else reason),
                membersFound=len(last),
                membersReady=ready,
                membersRestarted=restarted,
                members=last[:50],
            )
    return {"target": target_view(kind, cluster), "restart": out}


def zalando(ctx, kind, cluster, wait, dry, budget):
    """patronictl restart <cluster> <member> --force, member by member: replicas first, the leader
    last; each is waited for before the next."""
    requested_at = ctx.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    pod = pick_exec_pod(ctx, kind, load_pods(ctx, kind), "patronictl")
    raw, views = read_members(ctx, kind, pod)
    scope = patroni_cluster_name(raw)
    out = base(dry, "patronictl", requested_at, len(views))
    out.update(
        command={"tool": "patronictl", "subcommand": "restart", "cluster": scope, "member": None},
        ranIn={"pod": pod["metadata"]["name"], "container": KINDS[kind]["container"]},
    )
    results, last, stop = restart_members(ctx, kind, pod, scope, views, wait, budget, dry)
    out["memberResults"] = results[:50]
    if dry:
        return {"target": target_view(kind, cluster, scope), "restart": out}
    restarted = sum(1 for r in results if r["status"] == "restarted")
    out.update(
        applied=any(r["applied"] for r in results),
        completed=stop is None,
        completionReason="running" if stop is None else stop,
        waitedSeconds=round(sum(r["waitedSeconds"] or 0 for r in results), 1),
        membersFound=len(last),
        membersReady=sum(1 for m in last if m["running"]),
        membersRestarted=restarted,
        members=[
            {k: m[k] for k in ("name", "role", "state", "timeline", "lagMb", "pendingRestart")}
            for m in last[:50]
        ],
    )
    if stop == "not_observed":
        out["completed"] = None
    return {"target": target_view(kind, cluster, scope), "restart": out}


def base(dry, method, requested_at, expected):
    return {
        "applied": False if method == "patronictl" else not dry,
        "wouldRun": dry,
        "method": method,
        "requestedAt": requested_at,
        "membersExpected": expected,
        "annotation": None,
        "previousAnnotation": None,
        "command": None,
        "ranIn": None,
        "completed": None,
        "completionReason": None,
        "waitedSeconds": None,
        "membersFound": None,
        "membersReady": None,
        "membersRestarted": None,
        "members": None,
        "memberResults": None,
    }
