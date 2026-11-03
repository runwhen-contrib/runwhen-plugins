"""Fetching the target workload, its pods, and running redis-cli in a pod (the only I/O besides
k8s.py and project.py)."""

import subprocess
import time

from k8s_redis_health.budget import Budget
from k8s_redis_health.errors import RwError
from k8s_redis_health.k8s import MESSAGE_LIMIT, failure, run_unchecked
from k8s_redis_health.project import POD, REPLICASET, WORKLOAD, kubectl_projected
from k8s_redis_health.redis import (
    DEFAULT_PORT,
    EXEC_SCRIPT,
    auth_hint,
    auth_line,
    check_port,
    info_error,
    parse_info,
    reply_text,
)
from k8s_redis_health.workload import (
    DEPLOYMENT,
    owned_pods,
    pod_ready,
    resolve_target,
    selector_string,
)

EXEC_TIMEOUT = 10  # seconds for one redis-cli call, at most (the task's budget may allow less)


class Conn:
    """How redis-cli reaches Redis in this run: the port, and the task's time budget."""

    def __init__(self, port=DEFAULT_PORT, budget=None):
        self.port = check_port(port)
        self.budget = budget or Budget()


def target(ctx):
    """(Kind, kubectl resource) of the target. Needs no cluster call, so it runs first."""
    return resolve_target(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def load_workload(ctx, resource):
    """The workload (name, uid, selector only: its spec's env is never fetched). A missing target
    raises (NotFound from kubectl)."""
    return kubectl_projected(
        ctx, WORKLOAD, "-n", ctx.resource.namespace, "get", resource, ctx.resource.name
    )


def load_pods(ctx, kind, workload):
    """The pods the workload owns (selector AND ownerReferences chain), sorted by name."""
    ns = ctx.resource.namespace
    selector = selector_string(workload["spec"]["selector"])
    replicasets = []
    if kind == DEPLOYMENT:
        replicasets = kubectl_projected(
            ctx, REPLICASET, "-n", ns, "get", "replicasets.apps", "-l", selector, many=True
        )["items"]
    items = kubectl_projected(ctx, POD, "-n", ns, "get", "pods", "-l", selector, many=True)["items"]
    return owned_pods(kind, workload, replicasets, items)


def ready_pods(pods):
    return [p for p in pods if pod_ready(p)]


def redis_cli(ctx, pod, container, conn, *args):
    """Run `redis-cli -p <port> <args>` in the pod's container and return (reply text, latency ms).

    The latency is the wall time of the whole `kubectl exec`, so it includes the exec round trip
    through the API server: an upper bound for the server's own latency. A command that failed to
    run (forbidden exec, pod gone, no redis-cli) raises RwError with its D64 code; an
    authentication refusal raises permission_denied with the reply line and a hint; a call made
    after the task's time budget is spent raises timeout without running. Any other Redis error
    reply (READONLY, LOADING, ...) is returned as the reply: it is a fact about the server.
    """
    timeout = conn.budget.timeout(EXEC_TIMEOUT)
    argv = [
        "kubectl",
        "-n",
        ctx.resource.namespace,
        "exec",
        pod["metadata"]["name"],
        "-c",
        container,
        "--",
        "sh",
        "-c",
        EXEC_SCRIPT,
        "sh",
        "-p",
        str(conn.port),
        *args,
    ]
    started = time.monotonic()
    try:
        result = run_unchecked(ctx, argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"redis-cli did not answer within {timeout:g}s") from None
    latency = round((time.monotonic() - started) * 1000, 1)
    reply = reply_text(result.stdout)
    line = auth_line(reply) or auth_line(result.stderr)
    if line:
        raise RwError(
            "permission_denied", f"Redis refused the request: {line}", hint=auth_hint(line)
        )
    if result.returncode:
        raise failure(argv, result)
    return reply, latency


def redis_info(ctx, pod, container, conn):
    """(parsed INFO fields, latency ms) of the default INFO sections (server and replication among
    them, on every Redis version). An error reply is a RwError with its code and the reply line."""
    reply, latency = redis_cli(ctx, pod, container, conn, "INFO")
    error = info_error(reply)
    if error:
        raise error
    return parse_info(reply), latency


def error_entry(exc):
    """{code, message} of a RwError, for an instance entry."""
    return {"code": exc.code, "message": str(getattr(exc, "message", exc))[:MESSAGE_LIMIT]}
