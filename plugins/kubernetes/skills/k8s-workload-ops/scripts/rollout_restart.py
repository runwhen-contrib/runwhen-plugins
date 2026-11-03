import json

from k8s_workload_ops.clock import reference_time
from k8s_workload_ops.errors import RwError
from k8s_workload_ops.fetch import (
    load_revision_state,
    load_workload,
    target_kind,
    wait_for_rollout,
)
from k8s_workload_ops.k8s import MESSAGE_LIMIT, kubectl_text
from k8s_workload_ops.workload import (
    SHORT,
    check_restartable,
    clean_reason,
    has_restart_reason,
    kubectl_duration,
    limit_text,
    parse_duration,
    restart_patch,
    target_view,
    update_strategy,
)

MAX_WAIT_SECONDS = 240  # the task timeout is 300s


def main(ctx, timeout="120s", reason=None):
    kind = target_kind(ctx)
    wait = parse_duration(timeout)
    if not 0 < wait <= MAX_WAIT_SECONDS:
        raise RwError(
            "invalid_input",
            f"timeout must be above 0 and at most {MAX_WAIT_SECONDS}s, got {timeout!r}",
        )
    reason = clean_reason(reason)
    ns = ctx.resource.namespace
    workload = load_workload(ctx, kind)
    check_restartable(kind, workload)
    before = load_revision_state(ctx, kind, workload)
    ref = f"{SHORT[kind]}/{ctx.resource.name}"
    dry = bool(ctx.dry_run)
    reason_before = has_restart_reason(workload)

    # One merge patch sets the pod template's restartedAt (what `kubectl rollout restart` sets) and
    # the reason annotation together (or clears an older reason), so no half-done state is possible.
    # The dry run sends the identical patch with --dry-run=server.
    restarted_at = reference_time(ctx).strftime("%Y-%m-%dT%H:%M:%SZ")
    patch = restart_patch(restarted_at, reason)
    kubectl_text(
        ctx,
        "-n",
        ns,
        "patch",
        ref,
        "--type=merge",
        "-p",
        json.dumps(patch),
        *(["--dry-run=server"] if dry else []),
    )

    outcome = {
        "completed": None,
        "completionReason": None,
        "message": None,
        "truncated": False,
        "waitedSeconds": None,
    }
    after = None
    if not dry:
        completed, why, message, truncated, waited = wait_for_rollout(
            ctx, ref, wait, kubectl_duration(wait)
        )
        outcome = {
            "completed": completed,
            "completionReason": why,
            "message": message,
            "truncated": truncated,
            "waitedSeconds": waited,
        }
        try:
            after = load_revision_state(ctx, kind, load_workload(ctx, kind))
        except RwError as exc:
            ctx.error(
                exc.code,
                limit_text(
                    f"restarted, but could not read the new revision: {exc.message}", MESSAGE_LIMIT
                )[0],
                scope="revision",
            )

    return {
        "target": target_view(kind, workload),
        "restart": {
            "applied": not dry,
            "reason": reason,
            "reasonRecorded": reason is not None and not dry,
            "reasonPresentBefore": reason_before,
            "reasonCleared": reason is None and reason_before and not dry,
            "restartedAt": None if dry else restarted_at,
            "updateStrategy": update_strategy(kind, workload),
            "revisionBefore": before["revision"],
            "revisionAfter": after["revision"] if after else None,
            **outcome,
        },
    }
