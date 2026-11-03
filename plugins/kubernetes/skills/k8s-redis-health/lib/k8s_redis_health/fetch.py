"""Fetching the target workload, its pods, and running redis-cli in a pod (the only I/O besides
k8s.py)."""

import subprocess
import time

from k8s_redis_health.errors import RwError
from k8s_redis_health.k8s import MESSAGE_LIMIT, failure, kubectl_json, run_unchecked
from k8s_redis_health.redis import (
    AUTH_HINT,
    EXEC_SCRIPT,
    auth_failure,
    check_info,
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

EXEC_TIMEOUT = 20  # seconds for one redis-cli call


def target(ctx):
    """(Kind, kubectl resource) of the target. Needs no cluster call, so it runs first."""
    return resolve_target(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def load_workload(ctx, resource):
    """The workload. A missing target raises (NotFound from kubectl)."""
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", resource, ctx.resource.name)


def load_pods(ctx, kind, workload):
    """The pods the workload owns (selector AND ownerReferences chain), sorted by name."""
    ns = ctx.resource.namespace
    selector = selector_string(workload["spec"]["selector"])
    replicasets = []
    if kind == DEPLOYMENT:
        replicasets = kubectl_json(ctx, "-n", ns, "get", "replicasets.apps", "-l", selector)[
            "items"
        ]
    items = kubectl_json(ctx, "-n", ns, "get", "pods", "-l", selector)["items"]
    return owned_pods(kind, workload, replicasets, items)


def ready_pods(pods):
    return [p for p in pods if pod_ready(p)]


def redis_cli(ctx, pod, container, *args):
    """Run `redis-cli <args>` in the pod's container and return (reply text, latency in ms).

    The latency is the wall time of the whole `kubectl exec`, so it includes the exec round trip
    through the API server: an upper bound for the server's own latency. A command that failed to
    run (forbidden exec, pod gone, no redis-cli) raises RwError with its D64 code; an
    authentication refusal raises permission_denied. Any other Redis error reply (READONLY,
    LOADING, ...) is returned as the reply: it is a fact about the server.
    """
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
        *args,
    ]
    started = time.monotonic()
    try:
        result = run_unchecked(ctx, argv, timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"redis-cli did not answer within {EXEC_TIMEOUT}s") from None
    latency = round((time.monotonic() - started) * 1000, 1)
    reply = reply_text(result.stdout)
    if auth_failure(reply) or auth_failure(result.stderr):
        raise RwError("permission_denied", f"Redis refused the credentials: {AUTH_HINT}")
    if result.returncode:
        raise failure(argv, result)
    return reply, latency


def redis_info(ctx, pod, container, *section):
    """(parsed INFO fields, latency ms). Section names narrow the INFO call."""
    reply, latency = redis_cli(ctx, pod, container, "INFO", *section)
    required = "role" if section == ("replication",) else "redis_version"
    return check_info(parse_info(reply), required), latency


def error_entry(exc):
    """{code, message} of a RwError, for an instance entry."""
    return {"code": exc.code, "message": str(getattr(exc, "message", exc))[:MESSAGE_LIMIT]}
