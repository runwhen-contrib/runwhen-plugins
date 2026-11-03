from datetime import datetime, timezone

from k8s_cronjob_health.common import cap
from k8s_cronjob_health.fetch import (
    check_duration,
    check_settings,
    list_cronjob_events_secondary,
    list_jobs_secondary,
    load_cronjob,
)
from k8s_cronjob_health.schedule import (
    ScheduleError,
    active_jobs,
    cronjob_events,
    evaluate,
    schedule_facts,
    unevaluated,
)


def main(ctx, since="1h"):
    check_duration(since)
    check_settings(ctx, "max_items", "late_grace_seconds")
    cronjob = load_cronjob(ctx)
    now = ctx.now() if hasattr(ctx, "now") else datetime.now(timezone.utc)
    try:
        evaluated = evaluate(cronjob, now, since, ctx.settings.late_grace_seconds)
    except ScheduleError as exc:
        ctx.error(exc.code, exc.message, scope="schedule")
        evaluated = unevaluated()
    if evaluated["neverFires"]:
        ctx.error("invalid_input", "schedule never fires", scope="schedule")
    # the Job list only fills in the active Jobs' details: skipped when none are active
    active_refs = cronjob.get("status", {}).get("active") or []
    jobs = list_jobs_secondary(ctx) if active_refs else []
    active = active_jobs(cronjob, jobs, now)
    events = list_cronjob_events_secondary(ctx, cronjob)
    return {
        "schedule": schedule_facts(cronjob, evaluated, active),
        "activeJobs": cap(active, ctx.settings.max_items),
        "events": None
        if events is None
        else cap(cronjob_events(events, since, now), ctx.settings.max_items),
    }
