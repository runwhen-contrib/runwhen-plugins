import json

from k8s_postgres_operator_ops.fetch import (
    load_cluster,
    load_pods,
    pick_exec_pod,
    read_members,
    run_patronictl,
    target_kind,
    wait_until,
)
from k8s_postgres_operator_ops.k8s import kubectl_text
from k8s_postgres_operator_ops.operator import (
    CRUNCHY,
    KINDS,
    RESTART_ANNOTATION,
    all_members_up,
    check_timeout,
    crunchy_restart_done,
    desired_instances,
    patroni_cluster_name,
    pod_summary,
    restart_patch,
    target_view,
)


def main(ctx, timeout="120s"):
    kind = target_kind(ctx)
    wait = check_timeout(timeout)
    cluster = load_cluster(ctx, kind)
    dry = bool(ctx.dry_run)
    if kind == CRUNCHY:
        return crunchy(ctx, kind, cluster, wait, dry)
    return zalando(ctx, kind, cluster, wait, dry)


def crunchy(ctx, kind, cluster, wait, dry):
    """Set the restart annotation on spec.metadata.annotations; the operator rolls the instances."""
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
    out.update(annotation=RESTART_ANNOTATION, previousAnnotation=previous)
    if not dry and wait <= 0:
        out["completionReason"] = "not_waited"
    elif not dry:
        # The restart is the operator's: completed only when every instance pod it asks for is
        # Ready and was restarted at or after this request, not merely Running (it may not have
        # begun yet).
        def observe():
            return [pod_summary(kind, p) for p in load_pods(ctx, kind)]

        last, reason, waited = wait_until(
            ctx,
            wait,
            observe,
            lambda s: crunchy_restart_done(s, desired, requested_at)[0],
        )
        out["waitedSeconds"] = waited
        if last is None:
            out["completionReason"] = "not_observed"
        else:
            _, restarted, ready = crunchy_restart_done(last, desired, requested_at)
            out.update(
                completed=None if reason == "not_observed" else reason == "done",
                completionReason="restarted" if reason == "done" else reason,
                membersFound=len(last),
                membersReady=ready,
                membersRestarted=restarted,
                members=last[:50],
            )
    return {"target": target_view(kind, cluster), "restart": out}


def zalando(ctx, kind, cluster, wait, dry):
    """patronictl restart <cluster> --force: restarts every member (Postgres, not the pods)."""
    requested_at = ctx.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    pod = pick_exec_pod(ctx, kind, load_pods(ctx, kind), "patronictl")
    raw, views = read_members(ctx, kind, pod)
    scope = patroni_cluster_name(raw)
    out = base(dry, "patronictl", requested_at, len(views))
    out.update(
        command={"tool": "patronictl", "subcommand": "restart", "cluster": scope, "member": None},
        ranIn={"pod": pod["metadata"]["name"], "container": KINDS[kind]["container"]},
    )
    if not dry:
        out["output"], out["outputTruncated"] = run_patronictl(
            ctx, kind, pod, "restart", scope, "--force"
        )
        if wait <= 0:
            out["completionReason"] = "not_waited"
            return {"target": target_view(kind, cluster, scope), "restart": out}
        last, reason, waited = wait_until(
            ctx,
            wait,
            lambda: read_members(ctx, kind, pod)[1],
            all_members_up,
        )
        out["waitedSeconds"] = waited
        if last is None:
            out["completionReason"] = "not_observed"
        else:
            out.update(
                completed=None if reason == "not_observed" else reason == "done",
                completionReason="running" if reason == "done" else reason,
                membersFound=len(last),
                membersReady=sum(1 for m in last if m["running"]),
                members=[
                    {k: m[k] for k in ("name", "role", "state", "timeline", "lagMb")}
                    for m in last[:50]
                ],
            )
    return {"target": target_view(kind, cluster, scope), "restart": out}


def base(dry, method, requested_at, expected):
    return {
        "applied": not dry,
        "wouldRun": dry,
        "method": method,
        "requestedAt": requested_at,
        "membersExpected": expected,
        "annotation": None,
        "previousAnnotation": None,
        "command": None,
        "ranIn": None,
        "output": None,
        "outputTruncated": False,
        "completed": None,
        "completionReason": None,
        "waitedSeconds": None,
        "membersFound": None,
        "membersReady": None,
        "membersRestarted": None,
        "members": None,
    }
