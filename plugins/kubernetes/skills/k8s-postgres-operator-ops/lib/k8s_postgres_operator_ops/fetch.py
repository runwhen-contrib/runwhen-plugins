"""Fetching the target and what a write needs to know about it, and running patronictl in a member
pod (the only I/O besides k8s.py)."""

import time

from k8s_postgres_operator_ops.errors import RwError
from k8s_postgres_operator_ops.k8s import (
    MESSAGE_LIMIT,
    kubectl_exec,
    kubectl_exec_both,
    kubectl_json,
)
from k8s_postgres_operator_ops.operator import (
    BUDGET_MARGIN_SECONDS,
    KINDS,
    MIN_STEP_SECONDS,
    TASK_TIMEOUT_SECONDS,
    WRITE_EXEC_SECONDS,
    exec_pod,
    limit_text,
    members,
    parse_patroni,
    pod_selector,
    resolve_kind,
    WRITE_SUCCESS,
    switch_failed,
    verify_write,
    write_result_lines,
)

POLL_SECONDS = 3


class Budget:
    """What is left of the task's time. `end` is the task timeout (or the runner's own deadline
    when it gives one) less a margin: nothing started may run past it, so a write that went out
    always gets its result returned."""

    def __init__(self, ctx):
        self.start = time.monotonic()
        end = self.start + TASK_TIMEOUT_SECONDS
        deadline = getattr(ctx, "deadline_at", None)
        if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
            end = min(end, deadline)
        self.end = end - BUDGET_MARGIN_SECONDS

    def remaining(self):
        return self.end - time.monotonic()

    def elapsed(self):
        return time.monotonic() - self.start

    def can_start_step(self):
        return self.remaining() >= MIN_STEP_SECONDS

    def wait_for(self, wanted):
        """(seconds to wait, capped): the requested wait, cut to what is left."""
        left = max(0.0, self.remaining())
        return (left, True) if wanted > left else (wanted, False)

    def exec_seconds(self):
        """How long one patronictl write may run: the usual limit, cut to what is left."""
        return max(1.0, min(float(WRITE_EXEC_SECONDS), self.remaining()))


def target_kind(ctx):
    """The operator Kind of the target. Needs no cluster call, so it runs before anything else."""
    return resolve_kind(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def load_cluster(ctx, kind):
    """The PostgresCluster / postgresql. A missing target raises (NotFound from kubectl)."""
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", KINDS[kind]["resource"], ctx.resource.name
    )


def load_pods(ctx, kind):
    """The cluster's database pods (selected by the operator's labels)."""
    return kubectl_json(
        ctx,
        "-n",
        ctx.resource.namespace,
        "get",
        "pods",
        "-l",
        pod_selector(kind, ctx.resource.name),
    )["items"]


def pick_exec_pod(ctx, kind, pods, what, avoid=()):
    """The pod to run `what` in, or an error naming why there is none (nothing was changed)."""
    pod = exec_pod(kind, pods, avoid)
    if pod is None:
        raise RwError(
            "not_found",
            f"no Running {KINDS[kind]['container']} container among the pods of {ctx.resource.name!r} "
            f"(selector {pod_selector(kind, ctx.resource.name)}) to run {what} in; nothing was changed",
        )
    return pod


def read_members(ctx, kind, pod):
    """(raw, views): `patronictl list -f json` run in the pod."""
    out = kubectl_exec(
        ctx, pod["metadata"]["name"], KINDS[kind]["container"], "patronictl", "list", "-f", "json"
    )
    raw = parse_patroni(out)
    return raw, members(raw)


def run_patronictl(ctx, kind, pod, budget, action, member, *args, scope=None):
    """Run a mutating patronictl command in the pod and read what it printed.

    `action` is restart or reinit (done only with a `Success:` line for `member` and no `Failed:`
    line) or switchover or failover (done unless a failure line is printed).
    Returns a dict: `ok` (the output says it was done), `reason` (why not), `code` (the D64 code
    recorded: `internal` for a Failed line or a missing Success line even though patronictl
    exited 0, `timeout` when it was still running at the limit; None when fine), `output` and
    `truncated` (the non-table lines, bounded). A non-zero exit raises RwError (the message says
    patronictl's reason); the cluster may be partly changed. Every reported failure is also
    recorded with ctx.error, scope `scope`."""
    try:
        out, err = kubectl_exec_both(
            ctx,
            pod["metadata"]["name"],
            KINDS[kind]["container"],
            "patronictl",
            *args,
            timeout=budget.exec_seconds(),
        )
    except RwError as exc:
        if exc.code != "timeout":
            raise
        ctx.error(
            "timeout",
            f"patronictl {args[0]} was still running when the time limit came; the result is unknown: {exc.message}"[
                :MESSAGE_LIMIT
            ],
            scope=scope,
        )
        return {
            "ok": False,
            "reason": exc.message,
            "code": "timeout",
            "output": None,
            "truncated": False,
        }
    text = "\n".join(part for part in (out.strip(), err.strip()) if part)
    shown, truncated = limit_text(write_result_lines(text) or text)
    if action in WRITE_SUCCESS:
        ok, reason = verify_write(action, text, member)
    else:  # switchover, failover: the leader read back is the proof; a failure line is a failure
        failed = switch_failed(text)
        ok, reason = failed is None, failed
    if not ok:
        ctx.error(
            "internal",
            f"patronictl {args[0]} exited 0 but did not confirm it: {reason}"[:MESSAGE_LIMIT],
            scope=scope,
        )
    return {
        "ok": ok,
        "reason": reason,
        "code": None if ok else "internal",
        "output": shown,
        "truncated": truncated,
    }


def wait_until(ctx, seconds, observe, done):
    """Poll `observe()` every few seconds until `done(result, elapsed)` or `seconds` pass.

    A failed read while waiting is reported once with ctx.error (scope wait) and ends the wait:
    the write already happened and its result must not be lost. Returns
    (last_result, reason, waited): reason is 'done', 'timeout' or 'not_observed'. Running out of
    time is a result, not a failure.
    """
    start = time.monotonic()
    result = None
    while True:
        try:
            result = observe()
        except RwError as exc:
            ctx.error(
                exc.code,
                f"could not watch for the result: {exc.message}"[:MESSAGE_LIMIT],
                scope="wait",
            )
            return result, "not_observed", round(time.monotonic() - start, 1)
        elapsed = time.monotonic() - start
        if done(result, elapsed):
            return result, "done", round(elapsed, 1)
        remaining = seconds - elapsed
        if remaining <= 0:
            return result, "timeout", round(elapsed, 1)
        time.sleep(min(POLL_SECONDS, remaining))
