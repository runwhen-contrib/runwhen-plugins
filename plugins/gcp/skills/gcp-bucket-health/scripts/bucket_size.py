import time

from gcp_bucket_health.settings import read_settings
from gcp_bucket_health.size import deadline_from, run


def main(ctx):
    deadline = deadline_from(time.monotonic())
    return run(ctx, read_settings(ctx.settings, "usage_threshold_tib", "size_timeout"), deadline)
