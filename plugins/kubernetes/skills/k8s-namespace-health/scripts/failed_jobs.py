from datetime import datetime, timezone

from k8s_namespace_health.common import cap
from k8s_namespace_health.fetch import list_items, load_namespace
from k8s_namespace_health.jobs import failed_jobs, job_counts


def main(ctx):
    namespace = load_namespace(ctx)
    jobs = list_items(ctx, namespace, "jobs.batch")
    now = datetime.now(timezone.utc)
    flagged = failed_jobs(jobs, now, ctx.settings.job_active_max_minutes)
    return {"jobs": cap(flagged, ctx.settings.max_items), "counts": job_counts(jobs)}
