"""Failed and long-running Jobs in a namespace."""

from k8s_namespace_health.common import parse_time, trunc
from k8s_namespace_health.owners import controller_ref


def _condition(job, ctype):
    for c in job.get("status", {}).get("conditions") or []:
        if c.get("type") == ctype and c.get("status") == "True":
            return c
    return None


def failed_jobs(jobs, now, active_max_minutes):
    """Jobs with a Failed=True condition, and active Jobs running longer than
    `active_max_minutes`. Failed Jobs carry the condition's reason
    (BackoffLimitExceeded, DeadlineExceeded, ...) whenever they failed; there is
    no look-back, a failed Job stays until it is deleted or its TTL expires.
    A Job owned by a CronJob names it in `cronJob`. Not capped.
    """
    out = []
    for job in jobs:
        meta, spec, status = job["metadata"], job.get("spec", {}), job.get("status", {})
        failed = _condition(job, "Failed")
        active = status.get("active") or 0
        started = parse_time(status.get("startTime"))
        suspended = bool(spec.get("suspend", False))
        active_minutes = None
        if active > 0 and started is not None and not suspended:
            active_minutes = int((now - started).total_seconds() // 60)
        long_running = active_minutes is not None and active_minutes > active_max_minutes
        if failed is None and not long_running:
            continue
        owner = controller_ref(job)
        message, cut = trunc(failed.get("message") if failed else None)
        out.append(
            {
                "name": meta["name"],
                "cronJob": owner["name"] if owner and owner.get("kind") == "CronJob" else None,
                "failed": failed is not None,
                "failureReason": failed.get("reason") if failed else None,
                "failureMessage": message,
                "truncated": cut,
                "failedAt": failed.get("lastTransitionTime") if failed else None,
                "failedPods": status.get("failed") or 0,
                "backoffLimit": spec.get("backoffLimit"),
                "activeDeadlineSeconds": spec.get("activeDeadlineSeconds"),
                "active": active,
                "startTime": status.get("startTime"),
                "activeMinutes": active_minutes,
                "longRunning": long_running,
            }
        )
    out.sort(key=lambda j: (j["failedAt"] or "", j["startTime"] or "", j["name"]), reverse=True)
    return out


def job_counts(jobs):
    return {
        "total": len(jobs),
        "failed": sum(1 for j in jobs if _condition(j, "Failed")),
        "active": sum(1 for j in jobs if (j.get("status", {}).get("active") or 0) > 0),
        "complete": sum(1 for j in jobs if _condition(j, "Complete")),
    }
