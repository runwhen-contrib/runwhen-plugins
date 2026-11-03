"""The run's reference time."""

from datetime import datetime, timezone


def reference_time(ctx):
    """ctx.now(): one aware UTC instant fixed for the run, so a replay is reproducible. The wall
    clock on an SDK whose context has no now()."""
    now = getattr(ctx, "now", None)
    return now() if callable(now) else datetime.now(timezone.utc)
