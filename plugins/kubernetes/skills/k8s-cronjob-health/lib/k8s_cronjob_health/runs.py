"""The Jobs a CronJob started: how each ended, how long it took, why it failed, and how its pods ended."""

from k8s_cronjob_health.common import (
    fmt_time,
    nested,
    parse_duration,
    parse_time,
    seconds_between,
    trunc,
)

SCHEDULED_ANNOTATION = "batch.kubernetes.io/cronjob-scheduled-timestamp"


def owned_jobs(jobs, cronjob):
    """Jobs with an ownerReference to this CronJob (matched by uid, so a recreated
    CronJob of the same name does not claim its predecessor's Jobs). Includes Jobs made
    by hand with `kubectl create job --from=cronjob/...`, which carry the same reference."""
    uid = cronjob["metadata"]["uid"]
    return [
        j
        for j in jobs
        if any(r.get("uid") == uid for r in j["metadata"].get("ownerReferences") or [])
    ]


def _condition(job, ctype):
    for c in job.get("status", {}).get("conditions") or []:
        if c.get("type") == ctype and c.get("status") == "True":
            return c
    return None


def job_status(job):
    """'succeeded' (Complete), 'failed' (Failed) or 'active' (neither yet: pending,
    running or suspended)."""
    if _condition(job, "Failed"):
        return "failed"
    if _condition(job, "Complete"):
        return "succeeded"
    return "active"


def started_at(job):
    """When the Job started: `status.startTime`, else its creation (a suspended or
    just-created Job has no start time yet)."""
    return parse_time(job.get("status", {}).get("startTime")) or parse_time(
        job["metadata"].get("creationTimestamp")
    )


def _termination(container_status):
    """The last time a container ended: its current terminated state, else the previous one."""
    state = container_status.get("state") or {}
    last = (state.get("terminated")) or (container_status.get("lastState") or {}).get("terminated")
    if not last:
        return None
    return {
        "reason": last.get("reason"),
        "exitCode": last.get("exitCode"),
        "signal": last.get("signal"),
        "finishedAt": last.get("finishedAt"),
    }


def _containers(pod):
    st = pod.get("status", {})
    rows = []
    for init, key in ((True, "initContainerStatuses"), (False, "containerStatuses")):
        for cs in st.get(key) or []:
            state = cs.get("state") or {}
            kind = next(iter(state), None)
            rows.append(
                {
                    "name": cs["name"],
                    "init": init,
                    "state": kind,
                    "waitingReason": (state.get("waiting") or {}).get("reason"),
                    "restartCount": cs.get("restartCount") or 0,
                    "lastTermination": _termination(cs),
                }
            )
    return rows


def job_pods(pods, job):
    """Pods owned by the Job (by uid) with how each container ended. Returns (rows, truncated)."""
    uid = job["metadata"]["uid"]
    rows = []
    for pod in pods:
        if not any(r.get("uid") == uid for r in pod["metadata"].get("ownerReferences") or []):
            continue
        containers, cut = nested(_containers(pod))
        rows.append(
            {
                "name": pod["metadata"]["name"],
                "phase": pod.get("status", {}).get("phase"),
                "containers": containers,
                "containersTruncated": cut,
            }
        )
    rows.sort(key=lambda p: p["name"])
    rows, cut = nested(rows)
    return rows, cut


def run_entry(job, pods, now):
    """One Job as a run. `pods` is the list of namespace Job pods, or None when they
    could not be listed (then `pods` is null in the entry)."""
    meta, spec, status = job["metadata"], job.get("spec", {}), job.get("status", {})
    state = job_status(job)
    failed = _condition(job, "Failed")
    complete = _condition(job, "Complete")
    started = started_at(job)
    finished = parse_time(status.get("completionTime"))
    if finished is None and failed:
        finished = parse_time(failed.get("lastTransitionTime"))
    if finished is None and complete:
        finished = parse_time(complete.get("lastTransitionTime"))
    message, cut = trunc(failed.get("message") if failed else None)
    pod_rows, pods_cut = (None, False) if pods is None else job_pods(pods, job)
    return {
        "name": meta["name"],
        "status": state,
        "scheduledTime": (meta.get("annotations") or {}).get(SCHEDULED_ANNOTATION),
        "startTime": fmt_time(started),
        "completionTime": status.get("completionTime"),
        "finishedAt": fmt_time(finished) if state != "active" else None,
        "durationSeconds": seconds_between(started, finished) if state != "active" else None,
        "runningSeconds": (
            seconds_between(started, now)
            if state == "active" and not spec.get("suspend", False)
            else None
        ),
        "suspended": bool(spec.get("suspend", False)),
        "activePods": status.get("active") or 0,
        "succeededPods": status.get("succeeded") or 0,
        "failedPods": status.get("failed") or 0,
        "backoffLimit": spec.get("backoffLimit"),
        "failureReason": failed.get("reason") if failed else None,
        "failureMessage": message,
        "pods": pod_rows,
        "truncated": cut or pods_cut or any(p["containersTruncated"] for p in pod_rows or []),
    }


def recent_runs(cronjob, jobs, pods, since, now):
    """(runs, counts) for the CronJob's Jobs that started within `since`, newest first.
    `counts` also covers the Jobs outside the window (`owned`, `activeBeforeWindow`,
    `latestStatus`) so an empty window can be told from a CronJob that never ran.
    Runs are not capped here."""
    owned = owned_jobs(jobs, cronjob)
    start = now - parse_duration(since)
    window = [j for j in owned if started_at(j) is not None and started_at(j) >= start]
    window.sort(key=lambda j: (started_at(j), j["metadata"]["name"]), reverse=True)
    runs = [run_entry(j, pods, now) for j in window]
    newest = max(owned, key=lambda j: (started_at(j), j["metadata"]["name"]), default=None)
    counts = {
        "owned": len(owned),
        "inWindow": len(runs),
        "succeeded": sum(1 for r in runs if r["status"] == "succeeded"),
        "failed": sum(1 for r in runs if r["status"] == "failed"),
        "active": sum(1 for r in runs if r["status"] == "active"),
        "activeBeforeWindow": sum(
            1 for j in owned if job_status(j) == "active" and j not in window
        ),
        "latestStatus": job_status(newest) if newest else None,
        "latestStartTime": fmt_time(started_at(newest)) if newest else None,
    }
    return runs, counts
