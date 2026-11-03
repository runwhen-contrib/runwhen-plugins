"""Fetching the target workload and what a write needs to know about it (the
only I/O besides k8s.py)."""

import time

from k8s_workload_ops.errors import RwError
from k8s_workload_ops.k8s import MESSAGE_LIMIT, classify, kubectl_json
from k8s_workload_ops.workload import (
    DEPLOYMENT,
    RESOURCE,
    classify_rollout_failure,
    limit_text,
    owned_pods,
    owned_revisions,
    resolve_kind,
    revision_state,
    revision_views,
    selector_string,
)


def target_kind(ctx):
    """The workload Kind of the target. Needs no cluster call, so it runs
    before anything else."""
    return resolve_kind(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def load_workload(ctx, kind):
    """The workload. A missing target raises (NotFound from kubectl)."""
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", RESOURCE[kind], ctx.resource.name)


def load_revision_items(ctx, kind, workload):
    """The workload's ReplicaSets (Deployment) or ControllerRevisions."""
    resource = "replicasets.apps" if kind == DEPLOYMENT else "controllerrevisions.apps"
    selector = selector_string(workload["spec"]["selector"])
    items = kubectl_json(ctx, "-n", ctx.resource.namespace, "get", resource, "-l", selector)[
        "items"
    ]
    return owned_revisions(workload, items)


def load_revision_views(ctx, kind, workload):
    return revision_views(kind, load_revision_items(ctx, kind, workload))


def load_revision_state(ctx, kind, workload):
    """{revision, images} of the workload now. A Deployment carries its
    revision on itself; a StatefulSet or DaemonSet needs its ControllerRevisions."""
    views = [] if kind == DEPLOYMENT else load_revision_views(ctx, kind, workload)
    return revision_state(kind, workload, views)


def load_owned_pods(ctx, kind, workload):
    """The pods the workload owns (selector AND ownerReferences chain)."""
    replicasets = load_revision_items(ctx, kind, workload) if kind == DEPLOYMENT else []
    selector = selector_string(workload["spec"]["selector"])
    items = kubectl_json(ctx, "-n", ctx.resource.namespace, "get", "pods", "-l", selector)["items"]
    return owned_pods(kind, workload, replicasets, items)


def load_hpas(ctx, override):
    """HorizontalPodAutoscalers in the namespace. If they cannot be listed the
    "is an autoscaler in charge?" guard cannot be evaluated, so the call fails
    (nothing is scaled) unless the caller overrode the guard; then the failure
    is reported and None returned (unknown)."""
    try:
        return kubectl_json(
            ctx, "-n", ctx.resource.namespace, "get", "horizontalpodautoscalers.autoscaling"
        )["items"]
    except RwError as exc:
        if not override:
            raise RwError(
                exc.code,
                f"could not check whether an autoscaler targets this workload, so nothing was scaled: {exc.message}"[
                    :MESSAGE_LIMIT
                ],
            ) from None
        ctx.error(
            exc.code,
            f"could not list HorizontalPodAutoscalers: {exc.message}"[:MESSAGE_LIMIT],
            scope="hpa",
            hint="grant list on horizontalpodautoscalers (autoscaling) in the target's namespace",
        )
        return None


def wait_for_rollout(ctx, kind_ref, seconds, kubectl_timeout):
    """`kubectl rollout status` with a timeout: (completed, reason, message, truncated, waited).

    A slow, stuck or unwatchable rollout is returned as data (completed False
    and a reason); any other failure of the status call (forbidden,
    unreachable) is reported with ctx.error and still returns, because the
    write already happened and its result must not be lost.
    """
    started = time.monotonic()
    result = ctx.run(
        [
            "kubectl",
            "-n",
            ctx.resource.namespace,
            "rollout",
            "status",
            kind_ref,
            f"--timeout={kubectl_timeout}",
        ],
        timeout=seconds + 30,
    )
    waited = round(time.monotonic() - started, 1)
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    message, truncated = limit_text(text)
    if not result.returncode:
        return True, "rolled_out", message, truncated, waited
    code = classify(result.returncode, result.stderr)
    reason, slow = classify_rollout_failure(result.stderr)
    # kubectl prints "timed out" after a failed watch too: a forbidden or unreachable status call
    # is an error, whatever else it says, never a "slow rollout"
    if slow and code in ("internal", "timeout", "not_found"):
        return False, reason, message, truncated, waited
    ctx.error(
        code, f"kubectl rollout status failed: {message}"[:MESSAGE_LIMIT], scope="rollout-status"
    )
    return False, "not_observed", message, truncated, waited
