from k8s_postgres_operator_ops.fetch import (
    load_cluster,
    load_pods,
    pick_exec_pod,
    read_members,
    run_patronictl,
    target_kind,
)
from k8s_postgres_operator_ops.k8s import MESSAGE_LIMIT
from k8s_postgres_operator_ops.errors import RwError
from k8s_postgres_operator_ops.operator import (
    KINDS,
    check_reinit,
    clean_member,
    patroni_cluster_name,
    target_view,
)


def main(ctx, member):
    kind = target_kind(ctx)
    member = clean_member(member)
    cluster = load_cluster(ctx, kind)
    pod = pick_exec_pod(ctx, kind, load_pods(ctx, kind), "patronictl", avoid=(member,))
    raw, views = read_members(ctx, kind, pod)
    scope = patroni_cluster_name(raw)
    before = check_reinit(views, member)  # refuses the leader and an unknown member
    command = {"tool": "patronictl", "subcommand": "reinit", "cluster": scope, "member": member}
    dry = bool(ctx.dry_run)

    output, truncated = None, False
    state_after, role_after = None, None
    if not dry:
        output, truncated = run_patronictl(ctx, kind, pod, "reinit", scope, member, "--force")
        # reinit only starts the re-clone (the member shows 'creating replica' for a while): read the
        # state once, do not wait. A failed read is reported; the write already happened.
        try:
            _, after_views = read_members(ctx, kind, pod)
            after = next((m for m in after_views if m["name"] == member), None)
            state_after = after["state"] if after else None
            role_after = after["role"] if after else None
        except RwError as exc:
            ctx.error(
                exc.code,
                f"reinit sent, but could not read the member back: {exc.message}"[:MESSAGE_LIMIT],
                scope="verify",
            )
    return {
        "target": target_view(kind, cluster, scope),
        "reinit": {
            "applied": not dry,
            "wouldRun": dry,
            "member": member,
            "role": before["role"],
            "stateBefore": before["state"],
            "lagBeforeMb": before["lagMb"],
            "leader": next((m["name"] for m in views if m["leader"]), None),
            "command": command,
            "ranIn": {"pod": pod["metadata"]["name"], "container": KINDS[kind]["container"]},
            "output": output,
            "outputTruncated": truncated,
            "stateAfter": state_after,
            "roleAfter": role_after,
        },
    }
