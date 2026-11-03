"""Fetching the target, its member pods and the facts read inside them (the only I/O besides k8s.py)."""

from k8s_postgres_operator_health.errors import RwError
from k8s_postgres_operator_health.k8s import (
    MESSAGE_LIMIT,
    kubectl_exec,
    kubectl_json,
    kubectl_json_scoped,
)
from k8s_postgres_operator_health.operator import (
    KINDS,
    exec_candidates,
    pod_selector,
    primary_pods,
    psql_json,
    resolve_kind,
)


def target_kind(ctx):
    """The operator Kind of the target. Needs no cluster call, so it runs before anything else."""
    return resolve_kind(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def load_cluster(ctx, kind):
    """The PostgresCluster / postgresql. A missing one raises (NotFound from kubectl)."""
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", KINDS[kind]["resource"], ctx.resource.name
    )


def selector(ctx, kind):
    return pod_selector(kind, ctx.resource.name)


def load_pods(ctx, kind):
    """The cluster's database pods, or None when they cannot be listed (forbidden: reported
    with ctx.error, scope members)."""
    doc, state = kubectl_json_scoped(
        ctx, "members", "-n", ctx.resource.namespace, "get", "pods", "-l", selector(ctx, kind)
    )
    return None if state == "denied" else doc["items"]


def require_pods(ctx, kind):
    """The cluster's database pods; a forbidden list fails the task (it has nothing without them)."""
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "pods", "-l", selector(ctx, kind)
    )["items"]


def queried(kind, pod):
    return {"pod": pod["metadata"]["name"], "container": KINDS[kind]["container"]}


def patroni_members_text(ctx, kind, pod):
    return kubectl_exec(
        ctx,
        pod["metadata"]["name"],
        KINDS[kind]["container"],
        "patronictl",
        "list",
        "-f",
        "json",
    )


def psql_document(ctx, kind, pod, sql, what):
    """Run one query in the pod's database container on its local socket (no credential) and
    return the JSON document it prints."""
    out = kubectl_exec(
        ctx,
        pod["metadata"]["name"],
        KINDS[kind]["container"],
        "psql",
        "-U",
        "postgres",
        "-X",
        "-q",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )
    return psql_json(out.strip(), what)


def no_member_pod(ctx, kind, what):
    """Report (ctx.error, scope members) that there was no pod to run `what` in."""
    ctx.error(
        "not_found",
        f"no Running {KINDS[kind]['container']} container among the pods of {ctx.resource.name!r} "
        f"(selector {selector(ctx, kind)}) to run {what} in"[:MESSAGE_LIMIT],
        scope="members",
        hint="the cluster has no running member pod; check cluster-status members",
    )


def find_primary(ctx, kind, what):
    """(pod, candidates): the Running pod labelled primary, best first. None primary raises
    not_found: the facts live in the primary and nothing else can stand in for it."""
    pods = require_pods(ctx, kind)
    found = primary_pods(kind, pods)
    if not found:
        have = len(exec_candidates(kind, pods))
        raise RwError(
            "not_found",
            f"no Running pod of {ctx.resource.name!r} carries the primary role label "
            f"({KINDS[kind]['role_label']}=master|primary) to run {what} in; "
            f"{have} other pod(s) are running. Check patroni-status.",
        )
    return found[0], len(found)
