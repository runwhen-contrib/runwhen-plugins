"""Current state of the pods in a namespace."""

from k8s_namespace_health.common import nested, trunc
from k8s_namespace_health.owners import index_objects, resolve_owner


def _condition(pod, ctype):
    for c in pod.get("status", {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def _container_statuses(pod):
    status = pod.get("status", {})
    for s in status.get("initContainerStatuses") or []:
        yield True, s
    for s in status.get("containerStatuses") or []:
        yield False, s


def _pending(pod):
    scheduled = _condition(pod, "PodScheduled")
    unscheduled = scheduled is not None and scheduled.get("status") == "False"
    message, cut = trunc(scheduled.get("message") if unscheduled else None)
    return {
        "scheduled": not unscheduled,
        "reason": scheduled.get("reason") if unscheduled else None,
        "message": message,
    }, cut


def _failed(pod):
    status = pod.get("status", {})
    terminated = []
    for is_init, s in _container_statuses(pod):
        t = (s.get("state") or {}).get("terminated")
        if t and t.get("exitCode") != 0:
            terminated.append(
                {
                    "container": s["name"],
                    "init": is_init,
                    "reason": t.get("reason"),
                    "exitCode": t.get("exitCode"),
                }
            )
    message, cut = trunc(status.get("message"))
    terminated, more = nested(terminated)
    return {
        "reason": status.get("reason"),
        "message": message,
        "terminated": terminated,
    }, cut or more


def _waiting(pod):
    out, cut_any = [], False
    for is_init, s in _container_statuses(pod):
        w = (s.get("state") or {}).get("waiting")
        if w is not None:
            message, cut = trunc(w.get("message"))
            out.append(
                {
                    "container": s["name"],
                    "init": is_init,
                    "reason": w.get("reason"),
                    "message": message,
                }
            )
            cut_any = cut_any or cut
    out, more = nested(out)
    return out, cut_any or more


def _restarts(pod, threshold):
    out = []
    for is_init, s in _container_statuses(pod):
        count = s.get("restartCount", 0)
        if count < 1 or count < threshold:
            continue
        last = (s.get("lastState") or {}).get("terminated")
        out.append(
            {
                "container": s["name"],
                "init": is_init,
                "restartCount": count,
                "lastTerminated": (
                    {
                        "reason": last.get("reason"),
                        "exitCode": last.get("exitCode"),
                        "finishedAt": last.get("finishedAt"),
                    }
                    if last
                    else None
                ),
            }
        )
    out, more = nested(out)
    return out, more


def unhealthy_pods(pods, replicasets, jobs, restart_threshold):
    """Pods that are not in a good state right now, whenever they got there.

    Judged on current state only, with no look-back: a pod that has been Pending
    or Failed for a week is as present as one that failed a minute ago. States:
    `pending` (phase Pending; `pending.reason` is the scheduler's reason when
    unscheduled), `failed` (phase Failed), `notReady` (Running with the Ready
    condition not True), `waiting` (a container is waiting, with its reason) and
    `restarts` (a container restarted at least `restart_threshold` times).
    Succeeded pods only appear for restarts. Most states first, then by name.
    Nested lists stop at 50 and messages at 1024 characters (`truncated`).
    `replicasets` and `jobs` may be None when they could not be listed: an owner
    that goes through one then stops at it with `resolved` false.
    Not capped: the caller bounds the list and reports the full count.
    """
    index = index_objects(ReplicaSet=replicasets or [], Job=jobs or [])
    out = []
    for pod in pods:
        status = pod.get("status", {})
        phase = status.get("phase")
        ready = _condition(pod, "Ready")
        is_ready = ready is not None and ready.get("status") == "True"
        waiting, waiting_cut = _waiting(pod)
        restarts, restarts_cut = _restarts(pod, restart_threshold)
        cut = waiting_cut or restarts_cut
        states = []
        if phase == "Pending":
            states.append("pending")
        if phase == "Failed":
            states.append("failed")
        if phase == "Running" and not is_ready:
            states.append("notReady")
        if waiting:
            states.append("waiting")
        if restarts:
            states.append("restarts")
        if not states:
            continue
        pending = failed = None
        if phase == "Pending":
            pending, pending_cut = _pending(pod)
            cut = cut or pending_cut
        if phase == "Failed":
            failed, failed_cut = _failed(pod)
            cut = cut or failed_cut
        out.append(
            {
                "name": pod["metadata"]["name"],
                "phase": phase,
                "ready": is_ready,
                "states": states,
                "owner": resolve_owner(pod, index),
                "node": pod.get("spec", {}).get("nodeName"),
                "created": pod["metadata"].get("creationTimestamp"),
                "terminating": pod["metadata"].get("deletionTimestamp") is not None,
                "pending": pending,
                "failed": failed,
                "waiting": waiting,
                "restarts": restarts,
                "truncated": cut,
            }
        )
    out.sort(key=lambda p: (-len(p["states"]), p["name"]))
    return out


def pod_counts(pods, unhealthy):
    """Totals before the maxItems cap. `unhealthy` is the uncapped count."""
    phases = {}
    for p in pods:
        phase = p.get("status", {}).get("phase") or "Unknown"
        phases[phase] = phases.get(phase, 0) + 1
    return {"total": len(pods), "unhealthy": unhealthy, "byPhase": phases}
