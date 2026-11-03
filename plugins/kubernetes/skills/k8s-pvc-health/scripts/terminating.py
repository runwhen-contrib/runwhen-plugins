from datetime import datetime, timezone

from k8s_pvc_health.fetch import load_mounting_optional
from k8s_pvc_health.pvc import cap, termination


def main(ctx):
    claim, mounting = load_mounting_optional(ctx)
    now = datetime.now(timezone.utc)
    return {
        "termination": termination(claim, now),
        "pods": None if mounting is None else cap(mounting, ctx.settings.max_items),
    }
