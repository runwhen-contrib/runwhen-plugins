"""Schedule facts of a CronJob: what the controller was asked to do, what it did, and what it missed.

`status.lastScheduleTime` is the *scheduled* time of the last Job the controller
created (not the wall-clock moment it did), so a fire time after it is a run that
was never started. The controller looks for fires after `lastScheduleTime` (or
the CronJob's creation, before its first run), starts at most one Job for the most
recent of them, and does nothing while the CronJob is suspended.
"""

from datetime import timedelta

from k8s_cronjob_health import cron
from k8s_cronjob_health.common import fmt_time, parse_duration, parse_time, seconds_between, trunc

# Past 100 missed schedules the controller only records a TooManyMissedTimes warning and
# still starts the latest one; counting stops at 101 so a long outage costs a bounded search.
MISSED_CAP = 101

EVALUATED_FIELDS = (
    "lastExpectedFire",
    "nextExpectedFire",
    "neverFires",
    "evaluatedTimeZone",
    "missedFires",
    "missedFiresCapped",
    "missedSince",
    "behindSchedule",
    "deadlineExceeded",
)


class ScheduleError(Exception):
    """The schedule could not be evaluated; `code` is the D64 error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def unevaluated():
    """The evaluated fields when the schedule could not be evaluated: all null (not known)."""
    return dict.fromkeys(EVALUATED_FIELDS)


def evaluate(cronjob, now, since, grace_seconds):
    """When the schedule should have fired and whether runs were missed.

    Returns the EVALUATED_FIELDS. `grace_seconds` is how late a fire may be before
    it counts as missed (the controller syncs every few seconds). A suspended
    CronJob expects nothing: no missed fires, `behindSchedule` false.
    Raises ScheduleError when the schedule cannot be parsed or its zone is unknown.
    """
    spec, status, meta = cronjob.get("spec", {}), cronjob.get("status", {}), cronjob["metadata"]
    try:
        schedule = cron.parse_schedule(spec.get("schedule"), spec.get("timeZone"))
    except cron.TimeZoneUnavailable as exc:
        raise ScheduleError("dependency", str(exc)) from exc
    except ValueError as exc:
        raise ScheduleError(
            "internal", f"cannot evaluate schedule {spec.get('schedule')!r}: {exc}"
        ) from exc
    created = parse_time(meta.get("creationTimestamp"))
    last = parse_time(status.get("lastScheduleTime"))
    earliest = last or created
    if earliest is None:
        raise ScheduleError(
            "internal", "CronJob has neither lastScheduleTime nor creationTimestamp"
        )
    suspended = bool(spec.get("suspend"))
    cutoff = now - timedelta(seconds=grace_seconds)

    next_fire = schedule.next(now, earliest)
    expected = schedule.prev(now, earliest)
    if expected is not None and created is not None and expected <= created:
        expected = None  # it has not fired since the CronJob was created

    result = {
        "lastExpectedFire": fmt_time(expected),
        "nextExpectedFire": None if suspended else fmt_time(next_fire),
        "neverFires": next_fire is None and not suspended,
        "evaluatedTimeZone": schedule.zone_name,
        "missedFires": 0,
        "missedFiresCapped": False,
        "missedSince": False,
        "behindSchedule": False,
        "deadlineExceeded": None,
    }
    if suspended:
        return result

    missed, cursor = 0, earliest
    while missed < MISSED_CAP:
        cursor = schedule.next(cursor, earliest)
        if cursor is None or cursor > cutoff:
            break
        missed += 1
    result["missedFires"] = missed
    result["missedFiresCapped"] = missed >= MISSED_CAP
    result["behindSchedule"] = missed > 0

    window_start = max(earliest, now - parse_duration(since))
    first_in_window = schedule.next(window_start, earliest)
    result["missedSince"] = first_in_window is not None and first_in_window <= cutoff

    deadline = spec.get("startingDeadlineSeconds")
    if deadline is not None:
        # the controller drops a slot once it is older than the deadline, however long the
        # grace period: with a deadline shorter than the grace, judge by the deadline
        result["deadlineExceeded"] = False
        latest = schedule.prev(now - timedelta(seconds=min(grace_seconds, deadline)), earliest)
        if latest is not None and latest > earliest:
            result["deadlineExceeded"] = (now - latest).total_seconds() > deadline
    return result


def active_jobs(cronjob, jobs, now):
    """The Jobs the CronJob lists as active (`status.active`), with their state from the
    Job objects. `jobs` None means the Jobs could not be listed: details are null."""
    by_name = None if jobs is None else {j["metadata"]["name"]: j for j in jobs}
    out = []
    for ref in cronjob.get("status", {}).get("active") or []:
        row = {
            "name": ref.get("name"),
            "exists": None,
            "suspended": None,
            "startTime": None,
            "runningSeconds": None,
            "activePods": None,
            "succeededPods": None,
            "failedPods": None,
        }
        if by_name is not None:
            job = by_name.get(ref.get("name"))
            row["exists"] = job is not None
            if job is not None:
                st = job.get("status", {})
                started = parse_time(st.get("startTime"))
                suspended = bool(job.get("spec", {}).get("suspend", False))
                row.update(
                    suspended=suspended,
                    startTime=st.get("startTime"),
                    runningSeconds=None if suspended else seconds_between(started, now),
                    activePods=st.get("active") or 0,
                    succeededPods=st.get("succeeded") or 0,
                    failedPods=st.get("failed") or 0,
                )
        out.append(row)
    return out


def event_time(event):
    """The last time an event was seen (events.k8s.io and core/v1 shapes)."""
    for key in ("lastTimestamp", "eventTime"):
        if event.get(key):
            return event[key]
    series = event.get("series") or {}
    return (
        series.get("lastObservedTime")
        or event.get("firstTimestamp")
        or (event.get("metadata", {}).get("creationTimestamp"))
    )


# Events the controller records on every run: one per Job created, completed or removed.
# They say nothing the Jobs themselves do not, and a minutely CronJob writes hundreds.
ROUTINE_REASONS = ("SuccessfulCreate", "SawCompletedJob", "SuccessfulDelete")


def cronjob_events(events, since, now):
    """Events about the CronJob last seen within `since`, newest first, without the
    routine per-Job ones (ROUTINE_REASONS). Includes the Normal `JobAlreadyActive` the
    controller writes when Forbid skips a run, and warnings such as MissSchedule,
    FailedNeedsStart, UnexpectedJob, TooManyMissedTimes, InvalidSchedule. Not capped."""
    start = now - parse_duration(since)
    rows = []
    for e in events:
        seen = parse_time(event_time(e))
        if seen is None or seen < start or e.get("reason") in ROUTINE_REASONS:
            continue
        message, cut = trunc(e.get("message"))
        rows.append(
            {
                "type": e.get("type"),
                "reason": e.get("reason"),
                "count": e.get("count") or (e.get("series") or {}).get("count") or 1,
                "firstSeen": e.get("firstTimestamp") or event_time(e),
                "lastSeen": fmt_time(seen),
                "message": message,
                "truncated": cut,
            }
        )
    rows.sort(key=lambda r: (r["lastSeen"], r["reason"] or ""), reverse=True)
    return rows


def schedule_facts(cronjob, evaluated, active):
    """The `schedule` output: the CronJob's own fields plus the evaluated ones."""
    spec, status, meta = cronjob.get("spec", {}), cronjob.get("status", {}), cronjob["metadata"]
    last_schedule = status.get("lastScheduleTime")
    last_success = status.get("lastSuccessfulTime")
    policy = spec.get("concurrencyPolicy") or "Allow"
    behind = evaluated["behindSchedule"]
    older = None
    if last_schedule:
        older = last_success is None or parse_time(last_success) < parse_time(last_schedule)
    return {
        "suspended": bool(spec.get("suspend")),
        "schedule": spec.get("schedule"),
        "timeZone": spec.get("timeZone"),
        "concurrencyPolicy": policy,
        "startingDeadlineSeconds": spec.get("startingDeadlineSeconds"),
        "creationTimestamp": meta.get("creationTimestamp"),
        "lastScheduleTime": last_schedule,
        "lastSuccessfulTime": last_success,
        "successOlderThanLastSchedule": older,
        "activeCount": len(active),
        "forbidBlocked": None
        if behind is None
        else (policy == "Forbid" and len(active) > 0 and behind),
        **evaluated,
    }
