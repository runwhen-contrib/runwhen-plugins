from datetime import datetime, timezone

from k8s_cronjob_health.common import cap
from k8s_cronjob_health.fetch import (
    check_duration,
    check_settings,
    list_jobs,
    list_pods_of_jobs_secondary,
    load_cronjob,
)
from k8s_cronjob_health.runs import owned_jobs, recent_runs


def main(ctx, since="24h"):
    check_duration(since)
    check_settings(ctx, "max_items")
    cronjob = load_cronjob(ctx)
    jobs = list_jobs(ctx)
    now = ctx.now() if hasattr(ctx, "now") else datetime.now(timezone.utc)
    # the pods only say how each run's containers ended: skipped when the CronJob has no Jobs
    pods = list_pods_of_jobs_secondary(ctx) if owned_jobs(jobs, cronjob) else []
    runs, counts = recent_runs(cronjob, jobs, pods, since, now)
    return {"runs": cap(runs, ctx.settings.max_items), "counts": counts}
