from k8s_flux_health.errors import RwError
from k8s_flux_health.fetch import load_object, target_kind, wait_for_handled
from k8s_flux_health.flux import (
    FORCE_AT,
    HELMRELEASE,
    KINDS,
    REQUESTED_AT,
    RESET_AT,
    reconcile_after,
    reconcile_annotation,
    request_timestamp,
    state_view,
    target_view,
    parse_duration,
)
from k8s_flux_health.k8s import kubectl_text

MAX_WAIT_SECONDS = 240  # the task timeout is 300s


def main(ctx, timeout="60s", reset=False, force=False):
    kind = target_kind(ctx)
    wait = parse_duration(timeout)
    if not 0 <= wait <= MAX_WAIT_SECONDS:
        raise RwError(
            "invalid_input",
            f"timeout must be between 0s and {MAX_WAIT_SECONDS}s, got {timeout!r}",
        )
    if (reset or force) and kind != HELMRELEASE:
        raise RwError(
            "invalid_input",
            "reset and force apply to a HelmRelease only; a Kustomization has no retry counter "
            "to reset and no one-off upgrade to force",
        )
    ns = ctx.resource.namespace
    obj = load_object(ctx, kind)
    state = state_view(kind, obj)
    if state["suspended"]:
        raise RwError(
            "invalid_input",
            f"{kind} {ns}/{ctx.resource.name} is suspended: Flux ignores reconcile requests "
            "while spec.suspend is true. Run kubernetes:k8s-flux-health/resume first.",
        )
    dry = bool(ctx.dry_run)
    before = (obj.get("status") or {}).get("lastHandledReconcileAt")
    previous = reconcile_annotation(obj)

    # The same annotation `flux reconcile` sets. The controller echoes the value into
    # status.lastHandledReconcileAt once it has handled the request. A dry run sends the
    # identical call as a server-side dry run and does not wait.
    requested_at = request_timestamp()
    # reset (flux reconcile hr --reset) and force (--force) are further annotations on the same call;
    # a reset carries the request's own timestamp, so the controller's lastHandledResetAt can be matched.
    stamps = [f"{REQUESTED_AT}={requested_at}"]
    if reset:
        stamps.append(f"{RESET_AT}={requested_at}")
    if force:
        stamps.append(f"{FORCE_AT}={requested_at}")
    kubectl_text(
        ctx,
        "-n",
        ns,
        "annotate",
        KINDS[kind]["resource"],
        ctx.resource.name,
        *stamps,
        "--overwrite",
        *(["--dry-run=server"] if dry else []),
    )

    completed, reason, waited, after = None, "dry_run" if dry else "not_waited", None, None
    if not dry and wait > 0:
        latest, reason, waited = wait_for_handled(
            ctx, kind, requested_at, wait, requested_at if reset else None
        )
        completed = reason == "handled" if reason != "not_observed" else None
        after = reconcile_after(kind, latest) if latest is not None else None

    return {
        "target": target_view(kind, obj),
        "reconcile": {
            "applied": not dry,
            "wouldApply": dry,
            "reset": bool(reset),
            "force": bool(force),
            "requestedAt": None if dry else requested_at,
            "previousRequestedAt": previous,
            "lastHandledBefore": before,
            "completed": completed,
            "completionReason": reason,
            "waitedSeconds": waited,
            "after": after,
        },
    }
