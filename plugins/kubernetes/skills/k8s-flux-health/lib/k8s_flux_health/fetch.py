"""Fetching the target and what a task needs to know about it (the only I/O besides k8s.py)."""

import json
import time

from k8s_flux_health.errors import RwError
from k8s_flux_health.flux import (
    KINDS,
    NESTED_LIMIT,
    dependency_refs,
    dependency_view,
    handled,
    is_reconciling,
    limit_text,
    resolve_kind,
    state_view,
    suspend_patch,
    target_view,
)
from k8s_flux_health.k8s import MESSAGE_LIMIT, kubectl_json, kubectl_json_scoped, kubectl_text

POLL_SECONDS = 2


def target_kind(ctx):
    """The Flux Kind of the target. Needs no cluster call, so it runs before anything else."""
    return resolve_kind(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def load_object(ctx, kind):
    """The target. A missing one raises (NotFound from kubectl)."""
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", KINDS[kind]["resource"], ctx.resource.name
    )


def load_dependencies(ctx, kind, obj):
    """(views, total) for spec.dependsOn, same Kind as the target.

    Only the target's own namespace is read (D47): a dependency in another namespace is
    reported unread with a note, never fetched. A dependency that does not exist is a fact
    (exists false). A forbidden read is reported once with ctx.error (scope dependencies); the
    remaining dependencies are then left unread rather than asked again.
    """
    refs = dependency_refs(obj)
    own = ctx.resource.namespace
    views, denied = [], False
    for name, namespace in refs[:NESTED_LIMIT]:
        if namespace != own:
            views.append(
                dependency_view(
                    name,
                    namespace,
                    None,
                    read=False,
                    note=f"not read: it is in namespace {namespace!r}, outside the target's namespace {own!r}",
                )
            )
            continue
        if denied:
            views.append(
                dependency_view(
                    name, namespace, None, read=False, note="not read: permission denied"
                )
            )
            continue
        dep, state = kubectl_json_scoped(
            ctx,
            "dependencies",
            "-n",
            namespace,
            "get",
            KINDS[kind]["resource"],
            name,
            missing_ok=True,
        )
        if state == "denied":
            denied = True
            views.append(
                dependency_view(
                    name, namespace, None, read=False, note="not read: permission denied"
                )
            )
        else:
            views.append(dependency_view(name, namespace, dep, read=True))
    return views, len(refs)


def wait_for_handled(ctx, kind, requested_at, seconds, reset_at=None):
    """Poll the target until the controller has handled the request and is no longer Reconciling,
    or `seconds` pass.

    The controller records lastHandledReconcileAt when it starts on the request, so the Ready
    condition read at that moment can still be the old one; polling goes on until Reconciling is
    not True. Returns (object, reason, waited): reason is 'handled' (handled and finished),
    'reconciling' (handled, still Reconciling when time ran out), 'timeout' (not handled), or
    'not_observed' (a read failed while waiting; reported with ctx.error scope `wait`, object is the
    last one read). Running out of time is a result, not a failure.
    """
    start = time.monotonic()
    obj = None
    while True:
        try:
            obj = load_object(ctx, kind)
        except RwError as exc:
            ctx.error(
                exc.code,
                limit_text(f"could not watch for the reconcile: {exc.message}", MESSAGE_LIMIT)[0],
                scope="wait",
            )
            return obj, "not_observed", round(time.monotonic() - start, 1)
        seen = handled(obj, requested_at, reset_at)
        if seen and not is_reconciling(obj):
            return obj, "handled", round(time.monotonic() - start, 1)
        remaining = seconds - (time.monotonic() - start)
        if remaining <= 0:
            return obj, "reconciling" if seen else "timeout", round(time.monotonic() - start, 1)
        time.sleep(min(POLL_SECONDS, remaining))


def set_suspend(ctx, suspend):
    """Make spec.suspend `suspend` and return the task's outputs.

    Nothing is sent when the object already is in that state. A real change is one merge
    patch; a dry run sends the identical patch as a server-side dry run and reports
    wouldChange. suspendedAfter is the value in the cluster after the task.
    """
    kind = target_kind(ctx)
    obj = load_object(ctx, kind)
    before = state_view(kind, obj)["suspended"]
    change = before != suspend
    dry = bool(ctx.dry_run)
    if change:
        kubectl_text(
            ctx,
            "-n",
            ctx.resource.namespace,
            "patch",
            KINDS[kind]["resource"],
            ctx.resource.name,
            "--type=merge",
            "-p",
            json.dumps(suspend_patch(suspend)),
            *(["--dry-run=server"] if dry else []),
        )
    after = before
    if change and not dry:
        # Read the object back instead of assuming the patch stuck: a webhook or a parent
        # Flux object may have changed it again. The write already happened, so a failed read is
        # reported (scope verify) and suspendedAfter is null.
        try:
            after = state_view(kind, load_object(ctx, kind))["suspended"]
        except RwError as exc:
            ctx.error(
                exc.code,
                limit_text(
                    f"patched, but could not read spec.suspend back: {exc.message}", MESSAGE_LIMIT
                )[0],
                scope="verify",
            )
            after = None
    return {
        "target": target_view(kind, obj),
        "suspension": {
            "applied": change and not dry,
            "changed": change and not dry,
            "wouldChange": change and dry,
            "suspendedBefore": before,
            "suspendedAfter": after,
        },
    }
