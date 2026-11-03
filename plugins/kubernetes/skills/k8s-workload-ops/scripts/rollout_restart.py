import json
from datetime import datetime, timezone

from k8s_workload_ops.errors import RwError
from k8s_workload_ops.fetch import load_revision_state, load_workload, target_kind, wait_for_rollout
from k8s_workload_ops.k8s import MESSAGE_LIMIT, kubectl_text
from k8s_workload_ops.workload import (
    RESTART_REASON_ANNOTATION,
    SHORT,
    check_restartable,
    clean_reason,
    kubectl_duration,
    parse_duration,
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
    restarted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dry = bool(ctx.dry_run)

    if dry:
        # `kubectl rollout restart` has no --dry-run: send the patch it sends, as a server-side dry run
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}
                }
            }
        }
        kubectl_text(
            ctx, "-n", ns, "patch", ref, "--type=merge", "-p", json.dumps(patch), "--dry-run=server"
        )
    else:
        kubectl_text(ctx, "-n", ns, "rollout", "restart", ref)

    recorded = False
    if reason is not None:
        annotate = [
            "-n",
            ns,
            "annotate",
            ref,
            f"{RESTART_REASON_ANNOTATION}={reason}",
            "--overwrite",
        ]
        try:
            kubectl_text(ctx, *annotate, *(["--dry-run=server"] if dry else []))
            recorded = not dry
        except RwError as exc:
            if dry:
                raise
            # the restart is done; keep its result and say the reason was not recorded
            ctx.error(
                exc.code,
                f"restarted, but could not record the reason: {exc.message}"[:MESSAGE_LIMIT],
                scope="reason-annotation",
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
                f"restarted, but could not read the new revision: {exc.message}"[:MESSAGE_LIMIT],
                scope="revision",
            )

    return {
        "target": target_view(kind, workload),
        "restart": {
            "applied": not dry,
            "reason": reason,
            "reasonRecorded": recorded,
            "restartedAt": restarted_at,
            "updateStrategy": update_strategy(kind, workload),
            "revisionBefore": before["revision"],
            "revisionAfter": after["revision"] if after else None,
            **outcome,
        },
    }
