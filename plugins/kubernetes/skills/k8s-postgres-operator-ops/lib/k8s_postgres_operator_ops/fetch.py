"""Fetching the target and what a write needs to know about it, and running patronictl in a member
pod (the only I/O besides k8s.py)."""

import time

from k8s_postgres_operator_ops.errors import RwError
from k8s_postgres_operator_ops.k8s import (
    MESSAGE_LIMIT,
    kubectl_exec,
    kubectl_json,
)
from k8s_postgres_operator_ops.operator import (
    KINDS,
    exec_pod,
    limit_text,
    members,
    parse_patroni,
    pod_selector,
    resolve_kind,
)

POLL_SECONDS = 3
WRITE_EXEC_TIMEOUT = 120


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


def run_patronictl(ctx, kind, pod, *args):
    """Run a mutating patronictl command in the pod; returns its bounded stdout. A non-zero exit
    raises RwError (the cluster may be partly changed: the message says patronictl's reason)."""
    out = kubectl_exec(
        ctx,
        pod["metadata"]["name"],
        KINDS[kind]["container"],
        "patronictl",
        *args,
        timeout=WRITE_EXEC_TIMEOUT,
    )
    return limit_text(out.strip())


def wait_until(ctx, seconds, observe, done):
    """Poll `observe()` every few seconds until `done(result)` or `seconds` pass.

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
        if done(result):
            return result, "done", round(time.monotonic() - start, 1)
        remaining = seconds - (time.monotonic() - start)
        if remaining <= 0:
            return result, "timeout", round(time.monotonic() - start, 1)
        time.sleep(min(POLL_SECONDS, remaining))
