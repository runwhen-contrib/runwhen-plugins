from datetime import datetime, timezone

from k8s_pvc_health.fetch import load_mounting
from k8s_pvc_health.pvc import cap, termination


def main(ctx):
    claim, _pods, mounting = load_mounting(ctx)
    now = datetime.now(timezone.utc)
    return {"termination": termination(claim, now), "pods": cap(mounting, ctx.settings.max_items)}
