"""The Zalando rolling restart: Patroni members one at a time, replicas first and the leader last.

Each member is restarted with its own `patronictl restart <cluster> <member> --force`, counted
done only when patronictl printed its `Success:` line and the member then runs or streams with
its `Pending restart` cleared; the next member is not touched before that. The first member that
fails, does not come back, or finds no time left stops the loop: the members after it are
reported `not_attempted`, never restarted blind (restarting the leader while a replica is down
would leave no member to take over).
"""

from k8s_postgres_operator_ops.errors import RwError
from k8s_postgres_operator_ops.fetch import read_members, run_patronictl, wait_until
from k8s_postgres_operator_ops.k8s import MESSAGE_LIMIT
from k8s_postgres_operator_ops.operator import limit_text, member_back, restart_order

# Member `status` values: planned (dry run), restarted (Success line, then back with the pending
# restart cleared), not_back (Success line, but not back within the wait), failed (a Failed
# line, no Success line, or patronictl exited non-zero), unconfirmed (patronictl was still
# running at the time limit), not_attempted (an earlier member stopped the loop).


def _result(order, view, status, **extra):
    detail, truncated = limit_text(extra.pop("detail", None))
    result = {
        "member": view["name"],
        "role": view["role"],
        "order": order,
        "status": status,
        "applied": False,
        "stateBefore": view["state"],
        "pendingRestartBefore": view["pendingRestart"],
        "stateAfter": None,
        "pendingRestartAfter": None,
        "waitedSeconds": None,
        "output": None,
        "outputTruncated": False,
        "detail": detail,
        "detailTruncated": truncated,
    }
    result.update(extra)
    return result


def restart_members(ctx, kind, pod, scope, views, wait, budget, dry):
    """Returns (results, last_views, stop): one result per member in restart order; `last_views`
    is the last `patronictl list` read (the one passed in when none was made); `stop` is None
    when every member was restarted, else why the loop ended early: member_failed, timeout,
    budget or not_observed."""
    by_name = {m["name"]: m for m in views}
    results, last, stop = [], views, None
    for index, name in enumerate(restart_order(views), start=1):
        view = by_name[name]
        if dry:
            results.append(_result(index, view, "planned"))
            continue
        if stop is None and not budget.can_start_step():
            stop = "budget"
        if stop is not None:
            why = {
                "member_failed": "an earlier member was not restarted",
                "timeout": "an earlier member was not back within the wait",
                "not_observed": "an earlier member could not be observed",
                "budget": "not enough of the task's time was left",
            }[stop]
            results.append(_result(index, view, "not_attempted", detail=f"not restarted: {why}"))
            continue
        scope_name = f"restart:{name}"
        try:
            sent = run_patronictl(
                ctx, kind, pod, budget, "restart", name, "restart", scope, name, "--force",
                scope=scope_name,
            )  # fmt: skip
        except RwError as exc:
            ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=scope_name)
            results.append(_result(index, view, "failed", detail=exc.message))
            stop = "member_failed"
            continue
        if not sent["ok"]:
            status = "unconfirmed" if sent["code"] == "timeout" else "failed"
            results.append(
                _result(
                    index, view, status, detail=sent["reason"],
                    output=sent["output"], outputTruncated=sent["truncated"],
                )
            )  # fmt: skip
            stop = "member_failed"
            continue
        seconds, capped = budget.wait_for(wait)
        seen, reason, waited = wait_until(
            ctx,
            seconds,
            lambda: read_members(ctx, kind, pod)[1],
            lambda current, _elapsed, name=name: member_back(current, name),
        )
        if seen is not None:
            last = seen
        after = next((m for m in last if m["name"] == name), None)
        extra = {
            "applied": True,
            "output": sent["output"],
            "outputTruncated": sent["truncated"],
            "waitedSeconds": waited,
            "stateAfter": after["state"] if after else None,
            "pendingRestartAfter": after["pendingRestart"] if after else None,
        }
        if reason == "done":
            results.append(_result(index, view, "restarted", **extra))
            continue
        stop = "not_observed" if reason == "not_observed" else ("budget" if capped else "timeout")
        text = {
            "not_observed": "restarted, but the member could not be read back",
            "budget": "restarted, but not back before the task's time ran out",
            "timeout": "restarted, but not running or streaming with its pending restart cleared within the wait",
        }[stop]
        results.append(_result(index, view, "not_back", detail=text, **extra))
    return results, last, stop
